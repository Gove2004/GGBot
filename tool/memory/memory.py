"""长期记忆的读写。"""

from __future__ import annotations

import asyncio

from ..base import Tool
from ggbot.util.text import to_str


class MemoryFetch(Tool):
    name = "memory_fetch"
    description = (
        "在长期记忆中检索，返回与 query 相关的记忆条目（默认语义检索，未配置向量服务时按关键词匹配）。"
        "每条带 kind：profile 是这个人的当前状态、event 是发生过的事；同一个人的状态以最新那条为准。"
        "对方问“你还记得吗”“上次说的那个”，或你想不起某个群友、某件事、某个话题的细节时先查它，不要凭空猜"
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
        # touch=True：查过一次算"想起来过"，这条记忆的淡出时钟从这里往后走
        results = await asyncio.to_thread(
            self.rt.memory.search, query, k, ctx.get("session_id"), 0.35, True
        )
        if not results:
            return {"ok": True, "results": [], "note": "没有找到相关记忆"}
        return {"ok": True, "results": results}


class MemorySave(Tool):
    name = "memory_save"
    min_level = 2
    description = (
        "把值得长期记住的信息写入记忆库。kind=profile 记某个人的当前状态（叫法、喜好、脾气、住处、"
        "在忙什么），kind=event 记发生过的一件事（约定、决定、已经发生的时刻）。"
        "内容要自带主语、能独立看懂；subject 写这条是关于谁的。"
        "同一件事的新说法会改写旧的那条，不用你先删；带截止日期的事（周五交稿、下周三考试）填 expires_in_days。"
        "不要记琐碎寒暄。对方提到关于自己的新信息、或让你记住什么时，主动写入，不用等对方提醒"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容，一句话完整表述（带上是谁）"},
            "kind": {"type": "string", "enum": ["profile", "event"],
                     "description": "profile=某人的当前状态（默认），event=发生过的一件事"},
            "subject": {"type": "string", "description": "这条是关于谁的，写对方常用的叫法；说不清就留空"},
            "importance": {"type": "integer", "description": "重要度 1-5，默认 1"},
            "expires_in_days": {"type": "integer",
                                "description": "多少天后过期，只给自带截止日期的事填（如约定、备考）"},
        },
        "required": ["content"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        content = to_str(args.get("content", "")).strip()
        if not content:
            return {"ok": False, "error": "content 为空"}
        kind = "event" if str(args.get("kind") or "").strip().lower() == "event" else "profile"
        try:
            days = int(args.get("expires_in_days") or 0)
        except (TypeError, ValueError):
            days = 0
        fact = {
            "content": content,
            "kind": kind,
            "subject": to_str(args.get("subject", "")).strip(),
            "importance": max(1, min(5, int(args.get("importance", 1) or 1))),
            "expires_in_days": days if 0 < days <= 3650 else None,
            "session_id": ctx.get("session_id"),
        }
        # 走 commit 而不是直写：先找库里相关旧条目，判该新增、改写还是把旧的作废
        out = await asyncio.to_thread(self.rt.memory.commit, [fact])
        if not out["written"] and not out["updated"]:
            return {"ok": True, "note": "这条和库里已有的记忆重复，没有再写一条"}
        return {"ok": True, "note": "已写入长期记忆" if out["written"] else "已更新已有的记忆",
                "retired": out["retired"]}


def get_tools(rt):
    return [MemoryFetch(rt), MemorySave(rt)]
