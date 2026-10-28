"""文本输出。"""

from __future__ import annotations

from ..base import Tool


class SendText(Tool):
    name = "send_text"
    description = (
        "向当前会话发送一条文本消息（你说话的唯一方式；主动开场、接话也用它）。"
        "一条消息只说一句话，不要用换行或空行把多句拼在一起；想连发多条就多次调用本工具"
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "要发送的文本内容，口语化短句"}},
        "required": ["content"],
    }
    prompt_hint = "想说几句话就多次调用 send_text，一条一句；不要用换行把多句拼成一条。"

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rt.send_text(ctx, args.get("content", ""))


def get_tools(rt):
    return [SendText(rt)]
