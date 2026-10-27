"""向量化记忆：SiliconFlow Embedding API（Qwen/Qwen3-Embedding-4B）嵌入。

- 嵌入走云端 OpenAI 兼容接口（POST /embeddings），无本地模型依赖
- 向量存 SQLite（float32 BLOB），检索用余弦相似度（个人机器人记忆量级，暴力检索足够）
- 整理：合并余弦相似度超过阈值的近似重复记忆
"""

from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger("ggbot.memory")


class MemoryStore:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        emb = cfg.embedding
        self.model = emb["model"]
        base_url = emb["base_url"]
        api_key = emb["api_key"]
        if not api_key:
            raise RuntimeError(
                "缺少 embedding.api_key：请在 config.json 配置，或设置环境变量 EMBEDDING_API_KEY"
            )
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

    def add(
        self,
        content: str,
        type_: str = "fact",
        importance: int = 1,
        session_id: str | None = None,
    ) -> str:
        """写入一条记忆（自动向量化）。"""
        vec = self.embed(content)
        return self.storage.add_memory(session_id, content, type_, importance, vec.astype(np.float32).tobytes())

    def search(self, query: str, k: int = 5, session_id: str | None = None) -> list[dict]:
        """向量检索：余弦相似度 top-k。"""
        q = self.embed(query).astype(np.float32)
        qn = float(np.linalg.norm(q))
        scored = []
        for m in self.storage.get_all_memories():
            blob = m.get("embedding")
            if not blob:
                continue
            v = np.frombuffer(blob, dtype=np.float32)
            denom = qn * float(np.linalg.norm(v))
            if denom < 1e-9:
                continue
            score = float(np.dot(q, v)) / denom
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
        vecs = [np.frombuffer(m["embedding"], dtype=np.float32) for m in memories if m.get("embedding")]
        keep = set(range(len(memories)))
        removed_ids: list[str] = []
        for i in range(len(memories)):
            if i not in keep:
                continue
            vi = vecs[i]
            ni = float(np.linalg.norm(vi))
            for j in range(i + 1, len(memories)):
                if j not in keep:
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
