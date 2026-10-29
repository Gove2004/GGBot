"""长期记忆的读写。"""

from __future__ import annotations

import asyncio

from ..base import Tool, to_str


class MemorySearch(Tool):
    name = "memory_search"
    description = (
        "在长期记忆中检索，返回与 query 相关的记忆条目（默认语义检索，未配置向量服务时按关键词匹配）。"
        "想不起某个群友、某件事、某个话题的细节时先用它查，不要凭空猜"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "想检索的内容，用自然语言描述"},
            "k": {"type": "integer", "description": "返回条数，默认 3"},
        },
        "required": ["query"],
    }
    prompt_hint = "遇到“你还记得吗”“上次说的那个”或想不起某人某事时，先 memory_search 再回答。"

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
    min_level = 2
    description = (
        "把值得长期记住的信息写入记忆库（例如某人的叫法、偏好、脾气、约定、重要日子、最近在忙的事）。"
        "内容要自带主语、能独立看懂；不要记琐碎寒暄。"
        "对方提到关于自己的新信息、或让你记住什么时，主动写入，不用等对方提醒"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容，一句话完整表述（带上是谁）"},
            "importance": {"type": "integer", "description": "重要度 1-5，默认 1"},
        },
        "required": ["content"],
    }
    prompt_hint = "对方聊到自己的名字/喜好/近况/约定时，主动 memory_write；别只记在脑子里。"

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
