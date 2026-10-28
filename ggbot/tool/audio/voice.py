"""语音输出：转发音频或 edge-tts 合成。"""

from __future__ import annotations

import time

from ..base import Tool


class SendVoice(Tool):
    name = "send_voice"
    description = (
        "发送一条语音。两种方式：source 传公网音频 URL（可用上下文中的 [语音 url=...] 转发），"
        "或 text 传入想说的话由系统 TTS 合成语音"
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "公网音频 URL 或本地音频文件路径"},
            "text": {"type": "string", "description": "要合成语音的文本（TTS），与 source 二选一"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        source = str(args.get("source") or "").strip()
        text = str(args.get("text") or "").strip()
        if not source and not text:
            return {"ok": False, "error": "需要提供 source（音频 URL/路径）或 text（TTS 文本）"}
        if source:
            return await self.rt.send_media(ctx, 3, source, "语音")
        try:
            import edge_tts

            out = self.rt.temp_dir("audio") / f"voice_{int(time.time() * 1000)}.mp3"
            com = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
            await com.save(str(out))
            return await self.rt.send_media(ctx, 3, str(out), "语音(TTS)")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"TTS 合成失败: {e}"}


def get_tools(rt):
    return [SendVoice(rt)]
