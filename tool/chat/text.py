"""文本输出（内部通道：模型看不到这个工具）。"""

from __future__ import annotations

from ..base import Tool


class SendText(Tool):
    name = "send_text"
    llm_visible = False
    description = (
        "运行时的文本发送通道，不暴露给模型：模型把话写在回复正文里，由系统逐行拆成消息调本工具发出。"
        "正文只写文字，[表情]/[语音]/[图片] 这类占位符会被拦掉或剔除，媒体要走 send_sticker、"
        "send_voice_clone、send_image/send_video。"
        "群聊里 @昵称 会变成真@，正文开头的 [引用 关键词] 会变成引用消息。"
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string",
                                   "description": "要发送的文本内容，口语化短句；可含 @昵称 与开头的 [引用 关键词]"}},
        "required": ["content"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rt.send_text(ctx, args.get("content", ""))


def get_tools(rt):
    return [SendText(rt)]
