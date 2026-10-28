"""向量化记忆：SiliconFlow Embedding API（Qwen/Qwen3-Embedding-4B）嵌入。

- 嵌入走云端 OpenAI 兼容接口（POST /embeddings），无本地模型依赖
- 向量存 SQLite（float32 BLOB），检索用余弦相似度（个人机器人记忆量级，暴力检索足够）
- 写入去重：同会话内余弦 ≥0.95 视为同一条，更新原条目而非新增
- 整理：合并余弦相似度超过阈值的近似重复记忆
"""

from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger("ggbot.memory")

DEDUP_SCORE = 0.95


class MemoryStore:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        emb = cfg.embedding
        self.model = emb["model"]
        base_url = emb["base_url"]
        api_key = emb["api_key"]
        if not api_key:
            raise RuntimeError("缺少 embedding.api_key：请在 db/config.json 的 embedding.api_key 配置")
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=0)
        self.batch_size = emb["batch_size"]

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
                logger.debug(
                    "embedding 请求 %d 条，维度 %d (模型 %s)", len(texts), len(rows[0].embedding), self.model
                )
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
        """写入一条记忆（自动向量化）；同会话近似重复则更新原条目。"""
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
            vec = self.embed(content).astype(np.float32)
        self.storage.update_memory(
            memory_id,
            content=content,
            importance=importance,
            embedding=vec.tobytes() if vec is not None else None,
        )
        logger.info("记忆已更新 id=%s（重新向量化=%s）", memory_id, vec is not None)
        return True

    def search(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        min_score: float | None = None,
    ) -> list[dict]:
        """向量检索：余弦相似度 top-k。

        session_id 给定时只取「全局记忆 + 本会话记忆」；为 None 时检索全部（面板/调试）。
        min_score 过滤低相关结果（None 表示不过滤）。
        """
        q = self.embed(query).astype(np.float32)
        qn = float(np.linalg.norm(q))
        scored = []
        for m in self.storage.get_all_memories():
            if session_id is not None:
                scope = m.get("session_id") or None
                if scope is not None and scope != session_id:
                    continue
            blob = m.get("embedding")
            if not blob:
                continue
            score = self._score(q, qn, np.frombuffer(blob, dtype=np.float32))
            if min_score is not None and score < min_score:
                continue
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [
            {
                "id": m["id"],
                "content": m["content"],
                "type": m["type"],
                "importance": m["importance"],
                "session_id": m["session_id"],
                "score": round(s, 4),
                "ts": m["ts"],
            }
            for s, m in scored[:k]
        ]

    def tidy(self, threshold: float = 0.95) -> int:
        """整理记忆：合并余弦相似度超过阈值的近似重复，保留更重要（importance 高/更新）的一条。"""
        memories = self.storage.get_all_memories()
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
        return len(removed_ids)

    def embed_status(self) -> dict:
        return {
            "provider": "siliconflow",
            "model": self.model,
            "total": len(self.storage.get_all_memories()),
        }
