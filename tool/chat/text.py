"""文本输出。"""

from __future__ import annotations

from ..base import Tool


class SendText(Tool):
    name = "send_text"
    description = (
        "向当前会话发送一条文本消息（你说话的唯一方式；主动开场、接话也用它）。"
        "一条消息只说一句话，不要用换行或空行把多句拼在一起；想连发多条就多次调用本工具。"
        "content 只写要说的文字，不要写 [表情]/[语音]/[图片] 占位符——它们不会变成媒体，"
        "发表情用 send_sticker，发语音用 send_voice_clone，发图/视频用 send_image/send_video。"
        "群聊里想真@某人就写 @昵称；要引用消息，在正文开头写 [引用 关键词]（只写 [引用] 则引用当前这条）"
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string",
                                   "description": "要发送的文本内容，口语化短句；可含 @昵称 与开头的 [引用 关键词]"}},
        "required": ["content"],
    }
    prompt_hint = ("想说几句话就多次调用 send_text，一条一句；不要用换行把多句拼成一条。"
                   "群聊可写 @昵称 变成真@；引用某条消息时正文以 [引用 关键词] 开头。")

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rt.send_text(ctx, args.get("content", ""))


def get_tools(rt):
    return [SendText(rt)]
