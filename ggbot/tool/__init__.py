"""技能工具：DeepSeek function calling 的 schema 定义、执行器与 agent 循环。

技能 = 官方 tools 参数，共 9 个：
- 输出：send_text / seed_message / send_image / send_voice / send_video / send_emoji
- 回合控制：end_call（主动结束本轮，不回复）
- 认知：memory_search / memory_write

回合语义：LLM 的纯文本输出不会作为回复发送（会被忽略）；必须通过工具调用行动——
send_text/seed_message 发送消息、end_call 结束本轮（或未调用任何工具时默认视为 end_call）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools")

NO_REPLY = "__NO_REPLY__"

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "send_text",
            "description": "向当前会话发送一条文本消息（你回复的主要方式；同一条回复想说多句可多次调用，或用一条带换行）",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "要发送的文本内容，口语化短句"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "seed_message",
            "description": "主动发送一条开场/引导消息（不等对方提问，自己挑起话题或暖场，如刚上线、冷场时）。与 send_text 一样会给当前会话发送文本，区别是它表达主动发起而非回应",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "要主动发送的文本内容"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "主动结束本轮工具调用：不发送任何消息。当你觉得不需要回复、没有值得说的话时调用它结束回合（不要输出纯文本，纯文本会被忽略）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_image",
            "description": "发送一张图片。source 传公网可访问的图片 URL（可直接使用群友刚发的图片 URL，即上下文中的 [图片 url=...]），或本地 Temp/ 目录下的图片文件路径",
            "parameters": {
                "type": "object",
                "properties": {"source": {"type": "string", "description": "图片公网 URL 或本地文件路径"}},
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_voice",
            "description": "发送一条语音。两种方式：source 传公网音频 URL（可用上下文中的 [语音 url=...] 转发），或 text 传入想说的话由 TTS 合成语音",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "公网音频 URL 或本地音频文件路径"},
                    "text": {"type": "string", "description": "要合成语音的文本（TTS），与 source 二选一"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_video",
            "description": "发送一段视频。source 传公网可访问的视频 URL（可使用群友刚发的视频 URL），或本地文件路径",
            "parameters": {
                "type": "object",
                "properties": {"source": {"type": "string", "description": "视频公网 URL 或本地文件路径"}},
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_emoji",
            "description": "发送一个表情/贴纸，参数为表情文字（如 😂 👍 ❤️），以文本形式发送",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "表情文字，如 😂 👍 ❤️"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "在长期记忆中按语义检索，返回与 query 相关的记忆条目。用于回忆关于某个群友、某件事、某个话题的过往信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "想检索的内容，用自然语言描述"},
                    "k": {"type": "integer", "description": "返回条数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": "把值得长期记住的信息写入记忆库（例如某人的偏好、约定、重要事件）。只记重要且长期有用的内容，不要记琐碎对话",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容，用一句话完整表述"},
                    "importance": {"type": "integer", "description": "重要度 1-5，默认 1"},
                },
                "required": ["content"],
            },
        },
    },
]


def _to_str(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


class ToolExecutor:
    """执行 function calling 调用。ctx 提供回复目标与会话上下文。"""

    def __init__(self, cfg, storage, memory, bot):
        self.cfg = cfg
        self.storage = storage
        self.memory = memory
        self.bot = bot
        self.temp_dir = cfg.temp_dir

    async def execute(self, name: str, args: dict, ctx: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"未知工具 {name}"}
        sid = session_label(ctx.get("session_id"))
        logger.debug("工具 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200],
                     extra={"session": sid})
        try:
            return await handler(args, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具 %s 执行失败", name, extra={"session": sid})
            ctx["send_error"] = str(e)  # 发送类工具失败标记，供回合收尾区分 skipped/failed
            return {"ok": False, "error": str(e)}

    # ---------- 输出工具 ----------

    async def _tool_send_text(self, args: dict, ctx: dict) -> dict:
        content = _to_str(args.get("content", "")).strip()
        if not content:
            return {"ok": False, "error": "content 为空"}
        seq = ctx.get("msg_seq", 1)
        await self.bot.send_text(ctx["target"], content, ctx["scene"], ctx.get("msg_id"), seq,
                                 ctx.get("event_id"))
        ctx["msg_seq"] = seq + 1  # 递增 msg_seq，支持同一消息多次回复
        ctx["sent_any"] = True    # 回合内已实际发送 → 收尾标记 replied 而非 skipped
        logger.info("send_text seq=%d passive=%s: %.80s",
                    seq, bool(ctx.get("event_id")), content,
                    extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "sent": content}

    async def _tool_send_emoji(self, args: dict, ctx: dict) -> dict:
        return await self._tool_send_text(args, ctx)

    async def _tool_seed_message(self, args: dict, ctx: dict) -> dict:
        # 主动开场/引导消息：发送方式与 send_text 相同，语义标记为主动发起
        r = await self._tool_send_text(args, ctx)
        if r.get("ok"):
            r["note"] = "已主动发送开场消息"
        return r

    async def _tool_end_call(self, args: dict, ctx: dict) -> dict:
        ctx["end_call"] = True
        return {"ok": True, "note": "本轮已主动结束，不发送消息"}

    async def _tool_send_image(self, args: dict, ctx: dict) -> dict:
        source = args.get("source", "")
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self._send_media(ctx, 1, source, "图片")

    async def _tool_send_video(self, args: dict, ctx: dict) -> dict:
        source = args.get("source", "")
        if not source:
            return {"ok": False, "error": "source 为空"}
        return await self._send_media(ctx, 2, source, "视频")

    async def _tool_send_voice(self, args: dict, ctx: dict) -> dict:
        source = args.get("source")
        text = args.get("text")
        if not source and not text:
            return {"ok": False, "error": "需要提供 source（音频 URL/路径）或 text（TTS 文本）"}
        if source:
            return await self._send_media(ctx, 3, source, "语音")
        # TTS 合成（edge-tts，输出 mp3；QQ 语音富媒体为 silk，若平台拒绝会返回错误）
        try:
            import edge_tts

            out = self.cfg.media_dir("voice") / f"voice_{int(time.time() * 1000)}.mp3"
            com = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
            await com.save(str(out))
            return await self._send_media(ctx, 3, str(out), "语音(TTS)")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"TTS 合成失败: {e}"}

    async def _send_media(self, ctx: dict, file_type: int, source: str, label: str) -> dict:
        seq = ctx.get("msg_seq", 1)
        ok, detail = await self.bot.upload_and_send_media(
            ctx["target"], ctx["scene"], file_type, source, ctx.get("msg_id"), seq
        )
        if ok:
            ctx["msg_seq"] = seq + 1
            ctx["sent_any"] = True
            logger.info("send_media %s seq=%d passive=%s: %s",
                        label, seq, bool(ctx.get("event_id")), detail,
                        extra={"session": session_label(ctx.get("session_id"))})
            return {"ok": True, "sent": f"{label}已发送: {detail}"}
        return {"ok": False, "error": detail}

    # ---------- 认知工具 ----------

    async def _tool_memory_search(self, args: dict, ctx: dict) -> dict:
        query = args.get("query", "")
        k = int(args.get("k", 3))
        results = await asyncio.to_thread(self.memory.search, query, k, ctx.get("session_id"), 0.35)
        if not results:
            return {"ok": True, "results": [], "note": "没有找到相关记忆"}
        return {"ok": True, "results": results}

    async def _tool_memory_write(self, args: dict, ctx: dict) -> dict:
        content = _to_str(args.get("content", "")).strip()
        if not content:
            return {"ok": False, "error": "content 为空"}
        importance = max(1, min(5, int(args.get("importance", 1))))
        mid = await asyncio.to_thread(
            self.memory.add, content, "fact", importance, ctx.get("session_id")
        )
        return {"ok": True, "memory_id": mid, "note": "已写入长期记忆"}


async def agent_loop(llm, executor, messages: list, ctx: dict, max_rounds: int = 6) -> str:
    """function calling 循环：LLM 决策 → 执行工具 → 结果回传 → 继续，直到结束回合。

    回合结束条件（返回 NO_REPLY，调用方不会把文本当回复发送）：
    - LLM 某轮未调用任何工具 → 默认 end_call，该轮纯文本被忽略；
    - LLM 显式调用 end_call → 立即结束；
    - 达到 max_rounds。
    回复内容由工具发送（send_text / seed_message / send_image 等），不依赖返回文本。
    """
    history = list(messages)
    for _round in range(max_rounds):
        resp = await asyncio.to_thread(llm.chat, history, TOOLS)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            # 未调用工具 = 默认 end_call；纯文本内容忽略（不发送）
            logger.debug("agent 未调用工具，默认 end_call（文本忽略）: %.60r", (msg.content or "")[:60])
            return NO_REPLY
        names = [tc.function.name for tc in calls]
        history.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in calls
        ]})
        for tc in calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await executor.execute(tc.function.name, args, ctx)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        if "end_call" in names:
            logger.debug("agent 显式 end_call 结束本轮")
            return NO_REPLY
    # 工具循环超限：兜底结束（不回复）
    return NO_REPLY
