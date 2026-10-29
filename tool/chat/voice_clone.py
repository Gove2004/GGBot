"""MiMo 语音克隆 Tool：用参考音色把文本合成为语音并发送。

配置/数据：tool/chat/audio/config.json + voice_ref.wav（与代码同分类目录）
运行产物：temp/tool/chat/audio/*.wav → *.silk（随 temp TTL 清理）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..base import Tool

logger = logging.getLogger("ggbot.tools.voice_clone")


def _to_silk(path: Path) -> Path:
    """QQ 语音富媒体要求 SILK：MiMo 输出 24kHz/单声道/16bit WAV，取 PCM 直接编码。"""
    import wave

    import pysilk

    with wave.open(str(path), "rb") as w:
        fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        if fmt != (24000, 1, 2):
            raise RuntimeError(
                f"语音需为 24kHz/单声道/16bit，实际 {fmt[0]}Hz/{fmt[1]}ch/{fmt[2] * 8}bit"
            )
        pcm = w.readframes(w.getnframes())
    out = path.with_suffix(".silk")
    out.write_bytes(pysilk.encode(pcm, 24000, sample_rate=24000))
    return out


class VoiceClone(Tool):
    name = "send_voice_clone"
    min_level = 2
    data_name = "chat/audio"
    description = (
        "用固定参考音色把一段话合成语音并发送（比文字更有温度）。"
        "适合撒娇、安慰、问候、道晚安、分享开心事；私聊比群聊更合适，正事不要用语音。"
        "一次聊天一条就够，文本控制在 2-3 句、10-25 字以内"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要合成的话，口语化短句（10-25 字最佳）"},
        },
        "required": ["text"],
    }
    default_config = {
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-tts-voiceclone",
        "output_format": "wav",
        "reference_voice_path": "voice_ref.wav",
        "timeout": 60,
        "max_retries": 2,
        "max_chars": 60,
    }
    prompt_hint = (
        "对方明显低落、求安慰、撒娇，或你要道晚安、分享开心事时，优先考虑发一条短语音，"
        "比只打字更有温度；语音要短、有情绪起伏，像随手录的；一次聊天不连发多条。"
    )

    def _ref_path(self) -> Path:
        raw = str(self.config.get("reference_voice_path") or "voice_ref.wav")
        p = Path(raw)
        return p if p.is_absolute() else self.rt.data_dir(self.key) / p

    def is_enabled(self) -> bool:
        if not super().is_enabled():
            return False
        return bool(self.config.get("api_key")) and self._ref_path().is_file()

    def _synth(self, text: str) -> Path:
        cfg = self.config
        ref = self._ref_path()
        voice_b64 = base64.b64encode(ref.read_bytes()).decode()
        payload = {
            "model": cfg.get("model") or "mimo-v2.5-tts-voiceclone",
            "messages": [
                {"role": "user", "content": "Hello, MiMo, have you had lunch?"},
                {"role": "assistant", "content": text},
            ],
            "audio": {
                "format": cfg.get("output_format") or "wav",
                "voice": f"data:audio/wav;base64,{voice_b64}",
            },
            "max_completion_tokens": 1024,
        }
        base = str(cfg.get("base_url") or "https://api.xiaomimimo.com/v1").rstrip("/")
        timeout = max(5, int(cfg.get("timeout") or 60))
        retries = max(1, int(cfg.get("max_retries") or 2))
        fmt = str(cfg.get("output_format") or "wav").lower()
        out = self.rt.temp_dir(self.key) / f"mimo_{int(time.time() * 1000)}.{fmt}"

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    f"{base}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {cfg['api_key']}",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                audio_data = data["choices"][0]["message"]["audio"]["data"]
                out.write_bytes(base64.b64decode(audio_data))
                if not out.exists() or out.stat().st_size == 0:
                    raise RuntimeError("MiMo 返回音频为空")
                return out
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")[:200]
                last_error = RuntimeError(f"MiMo API HTTP {e.code}: {body}")
            except Exception as e:  # noqa: BLE001
                last_error = e
            if attempt < retries:
                time.sleep(min(2 * attempt, 5))
        raise last_error or RuntimeError("MiMo 语音合成失败")

    async def run(self, args: dict, ctx: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text 为空"}
        limit = max(1, int(self.config.get("max_chars", 60) or 60))
        if len(text) > limit:
            return {"ok": False, "error": f"语音文本过长（{len(text)} 字），请控制在 {limit} 字以内"}
        try:
            path = await asyncio.to_thread(self._synth, text)
            path = await asyncio.to_thread(_to_silk, path)
        except Exception as e:  # noqa: BLE001
            logger.warning("语音克隆失败: %s", e)
            return {"ok": False, "error": f"语音生成失败: {e}"}
        return await self.rt.send_media(ctx, 3, str(path), "语音克隆")


def get_tools(rt):
    return [VoiceClone(rt)]
