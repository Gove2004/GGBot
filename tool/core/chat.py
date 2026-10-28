"""文本输出与回合控制。"""

from __future__ import annotations

from ..base import Tool


class SendText(Tool):
    name = "send_text"
    description = (
        "向当前会话发送一条文本消息（你回复的主要方式；同一条回复想说多句可多次调用，"
        "或用一条带换行）"
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "要发送的文本内容，口语化短句"}},
        "required": ["content"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rt.send_text(ctx, args.get("content", ""))


class SeedMessage(Tool):
    name = "seed_message"
    description = (
        "主动发送一条开场/引导消息（不等对方提问，自己挑起话题或暖场，如刚上线、冷场时）。"
        "与 send_text 一样会给当前会话发送文本，区别是它表达主动发起而非回应"
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "要主动发送的文本内容"}},
        "required": ["content"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        r = await self.rt.send_text(ctx, args.get("content", ""))
        if r.get("ok"):
            r["note"] = "已主动发送开场消息"
        return r


class EndCall(Tool):
    name = "end_call"
    description = (
        "主动结束本轮工具调用：不发送任何消息。当你觉得不需要回复、没有值得说的话时调用它结束回合"
        "（不要输出纯文本，纯文本会被忽略）"
    )

    async def run(self, args: dict, ctx: dict) -> dict:
        ctx["end_call"] = True
        return {"ok": True, "note": "本轮已主动结束，不发送消息"}


def get_tools(rt):
    return [SendText(rt), SeedMessage(rt), EndCall(rt)]
