"""长期记忆的读写。"""

from __future__ import annotations

import asyncio

from ..base import Tool, to_str


class MemorySearch(Tool):
    name = "memory_search"
    description = (
        "在长期记忆中按语义检索，返回与 query 相关的记忆条目。"
        "用于回忆关于某个群友、某件事、某个话题的过往信息"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "想检索的内容，用自然语言描述"},
            "k": {"type": "integer", "description": "返回条数，默认 3"},
        },
        "required": ["query"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        query = str(args.get("query") or "").strip()
        k = int(args.get("k", 3) or 3)
        results = await asyncio.to_thread(
            self.rt.memory.search, query, k, ctx.get("session_id"), 0.35
        )
        if not results:
            return {"ok": True, "results": [], "note": "没有找到相关记忆"}
        return {"ok": True, "results": results}


class MemoryWrite(Tool):
    name = "memory_write"
    description = (
        "把值得长期记住的信息写入记忆库（例如某人的偏好、约定、重要事件）。"
        "只记重要且长期有用的内容，不要记琐碎对话"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容，用一句话完整表述"},
            "importance": {"type": "integer", "description": "重要度 1-5，默认 1"},
        },
        "required": ["content"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        content = to_str(args.get("content", "")).strip()
        if not content:
            return {"ok": False, "error": "content 为空"}
        importance = max(1, min(5, int(args.get("importance", 1) or 1)))
        mid = await asyncio.to_thread(
            self.rt.memory.add, content, "fact", importance, ctx.get("session_id")
        )
        return {"ok": True, "memory_id": mid, "note": "已写入长期记忆"}


def get_tools(rt):
    return [MemorySearch(rt), MemoryWrite(rt)]
