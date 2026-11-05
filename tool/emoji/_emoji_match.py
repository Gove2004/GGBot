"""挑图：把 query 和每条素材描述放进同一个向量空间比余弦，向量指望不上时退回字面重合。

- 语义路：描述侧原样嵌入，query 侧包一层 instruct（Qwen3-Embedding 这类模型按这个格式做检索），
  余弦相似度低于 `min_score` 就当「库里没有贴的」——实测这家厂商分布很挤，完全不相干的说法也能拿到 0.47。
- 字面路（没配 embedding，或整库都还没算出向量）：复用记忆检索的二元组重合度，不设阈值。
- 挑选：只在「分数贴着最高分的那一小撮」里换着发，档内先挑本会话没发过的，再挑用得最少的，
  还是打平才随机——纯随机会把明显更贴的那张换掉，只取最高分又会连着发同一张。
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

import numpy as np

from ggbot.llm.memory import bigrams
from ggbot.util.logging_setup import clip

from ._emoji_store import EMBED_BATCH

logger = logging.getLogger("ggbot.tools.emoji")

NEAR_TIE = 0.05   # 与最高分相差不到这个数的才算同一档，随机只在这一档里进行
NEAR_KEEP = 3     # 没有贴的时回给模型的邻近项条数


class EmojiMatcher:
    """检索与挑选；素材、配置、嵌入服务都从 store 那侧拿，自己不留状态。"""

    def __init__(self, store):
        self.store = store

    # ---------- 向量 ----------

    @property
    def memory(self):
        mem = self.store.rt.memory
        return mem if mem is not None and getattr(mem, "enabled", False) else None

    @property
    def mode(self) -> str:
        """面板上要说清现在靠什么挑图。"""
        return "vector" if self.memory is not None else "keyword"

    def query_text(self, q: str) -> str:
        """query 侧包一层 instruct（文档侧不包）：Qwen3-Embedding 按这个格式做检索。"""
        instr = str(self.store.config.get("query_instruct") or "").strip()
        return f"Instruct: {instr}\nQuery: {q}" if instr else q

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        """文本 → 向量，分批送（整库首扫时几千条一起发会被厂商拒）。

        没配 embedding 返回空；某一批失败就到此为止，剩下的下次再补——已算出的照旧可用。
        """
        mem = self.memory
        if mem is None or not texts:
            return []
        out: list[np.ndarray] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            try:
                got = await asyncio.to_thread(mem.embed_many, batch)
            except Exception as e:  # noqa: BLE001
                logger.warning("表情向量生成失败，改用关键词匹配: %s", clip(str(e), 60))
                break
            out.extend(got)
            if len(got) < len(batch):
                break
        return out

    # ---------- 打分 ----------

    async def rank(self, query: str, items: list[dict],
                   limit: int) -> tuple[list[dict], list[dict]]:
        """→ (够贴的候选, 差一点的邻近项)，两路都按相似度从高到低排。

        邻近项只在语义路里有意义（就是没过阈值的那几张）：把它回给模型，
        它才知道库里大致有哪些说法，不会换个同义词再来一次。
        """
        q = (query or "").strip()
        if not q:
            return [{**it, "score": 0.0} for it in items[:limit]], []
        if self.memory is not None and any(it.get("vec") is not None for it in items):
            got = await self.embed([self.query_text(q)])
            if got:
                return self._cosine(items, got[0], limit)
        return self._keyword(items, q, limit), []

    def _cosine(self, items: list[dict], qv: np.ndarray,
                limit: int) -> tuple[list[dict], list[dict]]:
        qn = float(np.linalg.norm(qv))
        floor = float(self.store.config.get("min_score", 0.50) or 0)
        scored = []
        for it in items:
            vec = it.get("vec")
            if vec is None:
                continue  # 向量缺失的条目不参与相似度排序，避免瞎给
            denom = qn * float(np.linalg.norm(vec))
            if denom < 1e-9:
                continue
            scored.append({**it, "score": round(float(np.dot(qv, vec)) / denom, 4)})
        scored.sort(key=lambda r: -r["score"])
        hits = [r for r in scored if r["score"] >= floor]
        near = [r for r in scored if r["score"] < floor][:NEAR_KEEP]
        return hits[:limit], near

    @staticmethod
    def _keyword(items: list[dict], q: str, limit: int) -> list[dict]:
        """降级匹配：字面二元组重合度（各家的向量分布差很多，这里不设阈值）。"""
        qgrams = bigrams(q.lower())
        if not qgrams:
            return []
        rows = []
        for it in items:
            text = f"{it['desc']} {Path(it['file']).stem}".lower()
            hit = len(qgrams & bigrams(text))
            if hit:
                rows.append({**it, "score": round(hit / len(qgrams), 4)})
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit]

    # ---------- 挑选 ----------

    @staticmethod
    def _cold_key(row: dict) -> tuple:
        return (int(row.get("use_count") or 0), str(row.get("last_used_at") or ""))

    def choose(self, hits: list[dict], recent) -> dict | None:
        """同档里换着一张张发：躲开本会话刚发过的，再躲开发过的次数少的。"""
        if not hits:
            return None
        best = hits[0]["score"]
        band = [r for r in hits if r["score"] >= best - NEAR_TIE]
        pool = [r for r in band if r["file"] not in recent] or band
        coldest = min(self._cold_key(r) for r in pool)
        return random.choice([r for r in pool if self._cold_key(r) == coldest])
