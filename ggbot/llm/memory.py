"""向量化记忆：OpenAI 兼容 Embedding 接口（服务商与模型由 config.json 决定）。

- 嵌入走云端 OpenAI 兼容接口（POST /embeddings），无本地模型依赖
- 向量存 SQLite（float32 BLOB），检索用余弦相似度（个人机器人记忆量级，暴力检索足够）
- 写入去重：同会话内余弦 ≥0.95 视为同一条，更新原条目而非新增
- 整理：合并近似重复记忆，阈值随记忆条数放宽（0.95 起，最低 0.86）
- 未配置 embedding 时自动降级为关键词检索，不影响机器人启动
"""

from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger("ggbot.memory")

DEDUP_SCORE = 0.95

# 整理（tidy）阈值：记忆越多越宽松。条数一多，同一件事的不同说法很难再撞上 0.95，
# 固定阈值只会让整理越来越无效；按规模放宽，最低降到 TIDY_MIN，防止误合不相干条目。
TIDY_BASE = 0.95
TIDY_MIN = 0.86
TIDY_PER = 50  # 每多 50 条放宽 0.01


def _tidy_threshold(count: int) -> float:
    return max(TIDY_MIN, TIDY_BASE - (count // TIDY_PER) * 0.01)


def bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in text if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


class MemoryStore:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        emb = cfg.embedding
        self.model = emb["model"]
        self.base_url = emb["base_url"]
        self.batch_size = emb["batch_size"]
        self.enabled = emb["enabled"]
        self.client = None
        if self.enabled:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=emb["api_key"], base_url=self.base_url, timeout=60, max_retries=0
            )
        else:
            logger.info("embedding 未启用，长期记忆使用关键词检索")

    def embed(self, text: str) -> np.ndarray:
        """单条文本 → 向量。"""
        return self._request([text])[0]

    def embed_many(self, texts: list[str]) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._request(texts[i : i + self.batch_size]))
        return out

    def _request(self, texts: list[str]) -> list[np.ndarray]:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.embeddings.create(model=self.model, input=texts)
                rows = sorted(resp.data, key=lambda d: d.index)
                if not rows:
                    raise RuntimeError("embedding 返回为空")
                return [np.array(r.embedding, dtype=np.float32) for r in rows]
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("embedding 调用失败（第 %d 次）: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Embedding API 调用失败: {last_err}")

    @staticmethod
    def _score(q: np.ndarray, qn: float, v: np.ndarray) -> float:
        denom = qn * float(np.linalg.norm(v))
        if denom < 1e-9:
            return 0.0
        return float(np.dot(q, v)) / denom

    def _nearest(self, vec: np.ndarray, session_id: str | None, min_score: float) -> tuple[dict, float] | None:
        """同一作用域内最相似的一条（用于写入去重）。"""
        qn = float(np.linalg.norm(vec))
        best: tuple[dict, float] | None = None
        for m in self.storage.get_all_memories():
            if (m.get("session_id") or None) != (session_id or None):
                continue
            blob = m.get("embedding")
            if not blob:
                continue
            sim = self._score(vec, qn, np.frombuffer(blob, dtype=np.float32))
            if sim >= min_score and (best is None or sim > best[1]):
                best = (m, sim)
        return best

    def add(
        self,
        content: str,
        type_: str = "fact",
        importance: int = 1,
        session_id: str | None = None,
    ) -> str:
        """写入一条记忆（启用 embedding 时自动向量化；同会话近似重复则更新原条目）。"""
        if not self.enabled:
            return self.storage.add_memory(session_id, content, type_, importance, None)
        vec = self.embed(content).astype(np.float32)
        dup = self._nearest(vec, session_id, DEDUP_SCORE)
        if dup is not None:
            m, sim = dup
            self.storage.update_memory(
                m["id"], content=content,
                importance=max(int(m.get("importance") or 1), importance),
                embedding=vec.tobytes(),
            )
            logger.info("记忆去重（相似度 %.3f），更新既有条目 %s", sim, m["id"])
            return m["id"]
        return self.storage.add_memory(session_id, content, type_, importance, vec.tobytes())

    def update(
        self,
        memory_id: str,
        content: str | None = None,
        importance: int | None = None,
    ) -> bool:
        """编辑记忆（改内容会重新向量化），条目不存在返回 False。"""
        if not self.storage.get_memory(memory_id):
            return False
        vec = None
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("记忆内容不能为空")
            # 未启用 embedding 时清空旧向量，避免内容与向量不一致
            vec = self.embed(content).astype(np.float32) if self.enabled else b""
        self.storage.update_memory(
            memory_id,
            content=content,
            importance=importance,
            embedding=vec.tobytes() if isinstance(vec, np.ndarray) else vec,
        )
        logger.info("记忆已更新 id=%s（重新向量化=%s）", memory_id,
                    isinstance(vec, np.ndarray))
        return True

    def _keyword_search(self, query: str, k: int, session_id: str | None) -> list[dict]:
        """无 embedding 时的降级检索：按字符/二元组重合度排序。"""
        q = (query or "").strip().lower()
        if not q:
            return []
        qgrams = bigrams(q)
        qchars = {ch for ch in q if not ch.isspace()}
        scored: list[tuple[float, dict]] = []
        for m in self.storage.get_all_memories():
            if session_id is not None:
                scope = m.get("session_id") or None
                if scope is not None and scope != session_id:
                    continue
            text = (m.get("content") or "").lower()
            if not text:
                continue
            overlap = len(qgrams & bigrams(text))
            hits = len(qchars & set(text))
            if not overlap and hits < 2:
                continue
            score = max(
                overlap / max(1, len(qgrams)),
                hits / max(1, len(qchars)) * 0.6,
            )
            scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], -int(x[1].get("importance") or 0)))
        return [self._result(m, s) for s, m in scored[:k]]

    @staticmethod
    def _result(m: dict, score: float) -> dict:
        return {
            "id": m["id"],
            "content": m["content"],
            "type": m["type"],
            "importance": m["importance"],
            "session_id": m["session_id"],
            "score": round(score, 4),
            "ts": m["ts"],
        }

    def search(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        min_score: float | None = None,
    ) -> list[dict]:
        """检索记忆：启用 embedding 时按余弦相似度，否则按关键词重合度。

        session_id 给定时只取「全局记忆 + 本会话记忆」；为 None 时检索全部（面板/调试）。
        min_score 只对向量检索生效。
        """
        if not self.enabled:
            return self._keyword_search(query, k, session_id)
        candidates = []
        for m in self.storage.get_all_memories():
            if session_id is not None:
                scope = m.get("session_id") or None
                if scope is not None and scope != session_id:
                    continue
            if m.get("embedding"):
                candidates.append(m)
        if not candidates:
            return self._keyword_search(query, k, session_id)
        q = self.embed(query).astype(np.float32)
        qn = float(np.linalg.norm(q))
        scored = []
        for m in candidates:
            score = self._score(q, qn, np.frombuffer(m["embedding"], dtype=np.float32))
            if min_score is not None and score < min_score:
                continue
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [self._result(m, s) for s, m in scored[:k]]

    def tidy(self, threshold: float | None = None) -> int:
        """整理记忆：合并余弦相似度超过阈值的近似重复，保留更重要（importance 高/更新）的一条。

        threshold 不给就按当前记忆条数自动放宽（见 _tidy_threshold）。
        """
        if not self.enabled:
            return 0
        memories = self.storage.get_all_memories()
        if threshold is None:
            threshold = _tidy_threshold(len(memories))
        vecs: dict[int, np.ndarray] = {}
        for i, m in enumerate(memories):
            if m.get("embedding"):
                vecs[i] = np.frombuffer(m["embedding"], dtype=np.float32)
        keep = set(vecs)
        removed_ids: list[str] = []
        for i in sorted(vecs):
            if i not in keep:
                continue
            vi = vecs[i]
            ni = float(np.linalg.norm(vi))
            for j in sorted(vecs):
                if j <= i or j not in keep:
                    continue
                vj = vecs[j]
                denom = ni * float(np.linalg.norm(vj))
                if denom < 1e-9:
                    continue
                sim = float(np.dot(vi, vj)) / denom
                if sim >= threshold:
                    a, b = memories[i], memories[j]
                    if (a["importance"], a["ts"]) >= (b["importance"], b["ts"]):
                        keep.discard(j)
                        removed_ids.append(b["id"])
                    else:
                        keep.discard(i)
                        removed_ids.append(a["id"])
                        break
        self.storage.delete_memories(removed_ids)
        if removed_ids:
            logger.info("记忆整理: 共 %d 条 · 阈值 %.2f · 合并 %d 条",
                        len(memories), threshold, len(removed_ids))
        return len(removed_ids)

    def embed_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "provider": self.cfg.embedding.get("provider", ""),
            "model": self.model,
            "total": len(self.storage.get_all_memories()),
        }
