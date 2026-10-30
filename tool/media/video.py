"""视频输出。"""

from __future__ import annotations

from ..base import Tool


class SendVideo(Tool):
    name = "send_video"
    min_level = 2
    description = ("发送一段视频。source 传公网可访问的视频 URL（可使用群友刚发的视频 URL），或 temp/ 下的本地文件；"
                   "你并没有看过视频内容，别描述它")
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string", "description": "视频公网 URL 或本地文件路径"}},
        "required": ["source"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        source = str(args.get("source") or "").strip()
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self.rt.send_media(ctx, 2, source, "视频")


def get_tools(rt):
    return [SendVideo(rt)]
