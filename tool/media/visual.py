"""图片 / 视频输出。"""

from __future__ import annotations

from ..base import Tool


class SendImage(Tool):
    name = "send_image"
    description = (
        "发送一张图片。source 传公网可访问的图片 URL（可直接使用群友刚发的图片 URL，"
        "即上下文中的 [图片 url=...]），或本地图片文件路径（temp/tool 下的文件直接上传，无需公网地址）"
    )
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string", "description": "图片公网 URL 或本地文件路径"}},
        "required": ["source"],
    }
    prompt_hint = "群友刚发的图片可以直接转发（用消息里的 url 或 local），也可以发本地存图。"

    async def run(self, args: dict, ctx: dict) -> dict:
        source = str(args.get("source") or "").strip()
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self.rt.send_media(ctx, 1, source, "图片")


class SendVideo(Tool):
    name = "send_video"
    description = "发送一段视频。source 传公网可访问的视频 URL（可使用群友刚发的视频 URL），或本地文件路径"
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string", "description": "视频公网 URL 或本地文件路径"}},
        "required": ["source"],
    }
    prompt_hint = "群友刚发的视频可以直接转发（用消息里的 url），不要假装看过视频内容。"

    async def run(self, args: dict, ctx: dict) -> dict:
        source = str(args.get("source") or "").strip()
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self.rt.send_media(ctx, 2, source, "视频")


def get_tools(rt):
    return [SendImage(rt), SendVideo(rt)]
