"""图片输出。"""

from __future__ import annotations

from ..base import Tool


class SendImage(Tool):
    name = "send_image"
    min_level = 2
    description = (
        "发送一张图片。source 传公网可访问的图片 URL（可直接使用群友刚发的图片 URL，"
        "即上下文中的 [图片 url=...]），或 temp/ 与 tool/media/emoji/ 下的本地图片（直接上传，无需公网地址）"
    )
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string", "description": "图片公网 URL 或本地文件路径"}},
        "required": ["source"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        source = str(args.get("source") or "").strip()
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self.rt.send_media(ctx, 1, source, "图片")


def get_tools(rt):
    return [SendImage(rt)]
