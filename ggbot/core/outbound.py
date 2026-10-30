"""正文发送通道：模型唯一的说话方式就是把话写进回复正文，由这里逐行拆成消息发出去。

它不是 Tool（模型看不到、也不该看到），所以住在 ggbot 里：
「发送: <正文>」这行日志、@昵称 与 [引用 关键词] 的还原、被动失败降级主动，都在这一处。
"""

from __future__ import annotations

import asyncio
import logging
import re

from ggbot.util.logging_setup import clip
from ggbot.util.session import session_label
from ggbot.util.text import (
    AT_NAME_RE,
    PLACEHOLDER_INLINE_RE,
    PLACEHOLDER_RE,
    QUOTE_RE,
    to_str,
)

logger = logging.getLogger("ggbot.outbound")


class Outbound:
    """把一段正文发到当前会话：拆行、@ 与引用还原、入库、降级主动发送。"""

    def __init__(self, cfg, storage, bot):
        self.cfg = cfg
        self.storage = storage
        self.bot = bot

    async def send(self, ctx: dict, content) -> dict:
        content = to_str(content).strip()
        sid = ctx.get("session_id")
        extra = {"session": session_label(sid)}
        content, reference = self._parse_quote(content, ctx)
        if not content:
            return {"ok": False, "error": "content 为空"}
        if PLACEHOLDER_RE.match(content):
            logger.warning("正文只有占位符，已拦截不发送: %s", clip(content, 40), extra=extra)
            return {
                "ok": False,
                "error": "不要用 [表情]/[语音]/[图片] 占位符；发表情调 send_sticker，"
                         "发语音调 send_voice_clone，发图/视频调 send_image/send_video",
            }
        cleaned = PLACEHOLDER_INLINE_RE.sub("", content).strip()
        if cleaned != content:
            # 只报被剔除的占位符，不复读整段正文（发送行里记的就是剔除后的干净正文）
            removed = " ".join(dict.fromkeys(PLACEHOLDER_INLINE_RE.findall(content)))
            logger.warning("正文夹带占位符已剔除: %s（余文照常发送）", removed, extra=extra)
            content = cleaned
            if not content:
                return {"ok": False, "error": "content 只剩占位符，请改用媒体工具发送"}
        # 入库与回传给模型的都用这份"人看的"文本；发给 QQ 的 wire 只在群聊里把
        # @昵称 换成官方 at 标记（标记只在 markdown 消息生效，见 QQBot.send_text）
        display = content
        is_group = ctx.get("scene") == "group"
        # 模型偶尔用换行/空行把多句拼进一条，这里拆成多条依次发送，
        # 避免出现"A 空行 B"式的整段回复
        segments = [s.strip() for s in re.split(r"\n+", display) if s.strip()] or [display]
        reply_cfg = getattr(self.cfg, "reply", {}) or {}
        gap = max(0.0, float(reply_cfg.get("segment_gap_sec", 2) or 0))
        for i, seg in enumerate(segments):
            wire = self._apply_mentions(seg, ctx) if is_group else seg
            seq = ctx.get("msg_seq", 1)
            kwargs = {"message_reference": reference} if reference else {}
            try:
                await self.bot.send_text(
                    ctx["target"], wire, ctx["scene"], ctx.get("msg_id"), seq,
                    ctx.get("event_id"), display=seg, **kwargs
                )
            except Exception as e:  # noqa: BLE001
                if not ctx.get("msg_id"):
                    raise
                # 被动回复失败（msg_id 过期等）→ 降级主动发送，确保送达；
                # 主动通道不带引用，避免已失效的消息 ID 再触发一次失败
                logger.warning("被动回复失败，降级主动发送: %s", e, extra=extra)
                await self.bot.send_text(ctx["target"], wire, ctx["scene"], None, seq,
                                         display=seg)
            ctx["msg_seq"] = seq + 1
            ctx["sent_any"] = True
            logger.info("发送: %s", clip(seg, 200), extra=extra)
            if sid:
                self.storage.add_message(sid, "assistant", "bot", seg, status="sent")
            if gap and i < len(segments) - 1:
                await asyncio.sleep(gap)
        # 送达与否只看有没有异常/降级行（同一信息只打一行），成功不再补确认行
        return {"ok": True, "sent": display}

    def _parse_quote(self, content: str, ctx: dict) -> tuple[str, dict | None]:
        """解析开头的 [引用 关键词]：回查本会话消息并生成 QQ message_reference。"""
        m = QUOTE_RE.match(content)
        if not m:
            return content, None
        head = (m.group("head") or "").strip()
        needle = (m.group(2) or "").strip()
        sid = ctx.get("session_id")
        target = None
        if needle and self.storage is not None and sid:
            target = self.storage.find_message_id(sid, needle)
        if not target:
            target = ctx.get("msg_id")
        body = content[m.end():].strip()
        ref = {"message_id": target, "ignore_get_message_error": True} if target else None
        return " ".join(p for p in (head, body) if p), ref

    def _apply_mentions(self, content: str, ctx: dict) -> str:
        """把 @昵称 换成 QQ 的 <qqbot-at-user id="member_openid" />；查不到的人保持原样。

        官方只在 markdown 消息里解析该标记，QQBot.send_text 负责切换通道，
        被拒时退回显示文本；入库与日志用的始终是 @昵称 的可读原文。
        """
        if self.storage is None:
            return content
        sid = ctx.get("session_id")

        def repl(m: re.Match) -> str:
            openid = self.storage.find_sender_openid(m.group(1), sid)
            return f'<qqbot-at-user id="{openid}" />' if openid else m.group(0)

        return AT_NAME_RE.sub(repl, content)
