"""QQ 消息收发网关：qq-botpy 事件接收、归一化、去重、富媒体落地、发送能力。

- 事件：群@（on_group_at_message_create）、单聊（on_c2c_message_create）、群全量（on_group_message_create）
- 富媒体附件（图片/语音/视频/文件）下载到 temp/ 并记录公网 URL（可转发）
- 发送：文本被动回复（msg_id + 递增 msg_seq）、富媒体上传（base64 file_data → file_info → msg_type=7）
- 日志：发送者显示 QQ 昵称 + openid 前 8 位（事件未带昵称时只显示 8 位 id），引用消息显示为
  [引用 被引内容] 正文；不调用名称解析接口，无白名单/网络依赖
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

import aiohttp
import botpy
from botpy.http import Route
from botpy.message import C2CMessage, GroupMessage

from ggbot.util.media import media_kind_label
from ggbot.util.session import oid8, session_label

logger = logging.getLogger("ggbot.bot")

# QQ 群消息 @ 人/机器人：content 内嵌 <@openid>（openid 为 32 位十六进制）
AT_RE = re.compile(r"<@([0-9A-Fa-f]{32})>")
# QQ 表情：<faceType=1,faceId="427",ext="<base64>">，ext 内含表情文字
FACE_RE = re.compile(r'<faceType=\d+,faceId="[^"]*"(?:,ext="([^"]*)")?\s*/?>')
MAX_INLINE_UPLOAD = 10 * 1024 * 1024


def _face_text(match: re.Match) -> str:
    text = ""
    try:
        data = json.loads(base64.b64decode(match.group(1) or "").decode("utf-8"))
        if isinstance(data, dict):
            text = str(data.get("text") or "").strip()
    except (ValueError, UnicodeDecodeError):
        pass
    return f"[表情:{text}]" if text else "[表情]"


def clean_content(content: str) -> str:
    """剥离 QQ 富文本标记：表情还原为文字，@ 标记去掉（是否被@由 is_at 单独标识）。"""
    text = FACE_RE.sub(_face_text, content or "")
    text = AT_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def attachment_summary(attachments) -> str:
    """附件概要：[图片 cat.gif] [语音 a.silk]，用于无文本消息的日志/展示。"""
    parts = []
    for a in attachments or []:
        label = media_kind_label(getattr(a, "content_type", ""))
        name = (getattr(a, "filename", "") or "").strip()
        if len(name) > 32:
            name = name[:29] + "…"
        parts.append(f"[{label} {name}]" if name else f"[{label}]")
    return " ".join(parts)


def extract_quote(elements) -> str:
    """从 msg_elements 取被引用消息的文本（message_type=103，可嵌套）。"""
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        try:
            kind = int(el.get("message_type") or 0)
        except (TypeError, ValueError):
            kind = 0
        if kind == 103:
            text = clean_content(el.get("content") or "")
            if text:
                return text[:200] + ("…" if len(text) > 200 else "")
        nested = extract_quote(el.get("msg_elements"))
        if nested:
            return nested
    return ""


class _RichGroupMessage(GroupMessage):
    """保留 botpy 丢弃的 author.username / message_type / msg_elements 字段。"""

    __slots__ = ("username", "msg_elements", "message_type", "message_scene", "quote_text")

    def __init__(self, api, event_id, data: dict):
        super().__init__(api, event_id, data)
        self.username = ((data.get("author") or {}).get("username") or "").strip()
        self.msg_elements = data.get("msg_elements") or []
        self.message_type = data.get("message_type")
        self.message_scene = data.get("message_scene") or {}
        self.quote_text = extract_quote(self.msg_elements)


class _RichC2CMessage(C2CMessage):
    __slots__ = ("username", "msg_elements", "message_type", "message_scene", "quote_text")

    def __init__(self, api, event_id, data: dict):
        super().__init__(api, event_id, data)
        self.username = ((data.get("author") or {}).get("username") or "").strip()
        self.msg_elements = data.get("msg_elements") or []
        self.message_type = data.get("message_type")
        self.message_scene = data.get("message_scene") or {}
        self.quote_text = extract_quote(self.msg_elements)


class QQBot(botpy.Client):
    def __init__(self, config, presence=None, intents=None):
        # bot_log=None 保留 botpy 日志向根 logger 传播；ext_handlers=False 关闭
        # botpy 默认文件 handler（否则会在 cwd 生成 botpy.log）。
        super().__init__(intents=intents, timeout=30, bot_log=None, ext_handlers=False)
        self.cfg = config
        self.presence = presence
        self.temp_dir: Path = config.temp_dir
        self._seen: set[str] = set()
        self._max_seen = 5000

    # ---------- 事件 ----------

    async def on_ready(self) -> None:
        self._register_message_parsers()
        logger.info("QQBot就绪: WebSocket gateway 已连接")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        # GROUP_AT_MESSAGE_CREATE：只有真正被@本机器人时触发（事件通道）
        self._learn_bot_openid(message)
        sid = session_label(f"group:{message.group_openid}")
        logger.info("群@[%s] %.80s", self._who(message), self._message_text(message),
                    extra={"session": sid})
        await self._route(message, is_at=True, passive_ok=True)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        # 私聊 = 直接找机器人对话，等价于 @；C2C 事件可被动回复
        openid = getattr(message, "author", None) and getattr(message.author, "user_openid", None)
        sid = session_label(f"c2c:{openid}")
        logger.info("私聊[%s] %.80s", self._who(message), self._message_text(message),
                    extra={"session": sid})
        await self._route(message, is_at=True, passive_ok=True)

    async def on_group_add_robot(self, message) -> None:
        gid = getattr(message, "group_openid", None)
        logger.info("机器人入群", extra={"session": session_label(f"group:{gid}")})

    # ---------- 消息审核事件（消息被平台审核拦截时推送，用于排查"受理但未展示"） ----------

    async def on_message_audit_pass(self, audit) -> None:
        logger.info("消息审核通过 message_id=%s audit_id=%s",
                    getattr(audit, "message_id", None), getattr(audit, "audit_id", None))

    async def on_message_audit_reject(self, audit) -> None:
        # 审核不通过：消息不会展示；对比我们发送返回的 resp_id 即可确认是哪条
        logger.error("消息审核不通过，未展示 message_id=%s audit_id=%s guild=%s channel=%s",
                     getattr(audit, "message_id", None), getattr(audit, "audit_id", None),
                     getattr(audit, "guild_id", None), getattr(audit, "channel_id", None))

    def _register_message_parsers(self) -> None:
        """注入群聊/私聊事件解析器，保留 botpy 丢弃的昵称与引用消息字段。

        botpy 1.2.1 未内置 GROUP_MESSAGE_CREATE parser（收到即 unknown event），
        且三个群/私聊消息模型都只取 openid，丢掉 author.username / msg_elements。
        这里统一换成 _Rich*Message 子类，原始字段在 _normalize 中落库与入上下文。
        """
        try:
            state = self._connection.state
            import types

            def make_parser(event: str, cls):
                def parse(_state, payload):
                    data = payload.get("d", {}) or {}
                    _state._dispatch(event, cls(_state.api, payload.get("id"), data))
                return parse

            for event, cls in (
                ("group_at_message_create", _RichGroupMessage),
                ("group_message_create", _RichGroupMessage),
                ("c2c_message_create", _RichC2CMessage),
            ):
                state.parsers[event] = types.MethodType(make_parser(event, cls), state)
        except Exception as e:  # noqa: BLE001
            logger.warning("注册消息解析器失败: %s", e)

    async def on_group_message_create(self, message: GroupMessage) -> None:
        """群全量消息（私域权限，所有普通消息；含@别人的消息，不含对本机器人的@）。

        is_at 判定：全量消息的 mentions / content <@openid> 识别是否@本机器人——
        优先精确匹配 config.qq.bot_openid；未学到时从第一条含 <@> 的消息自动学习
        （测试群机器人唯一），之后切回精确匹配。"""
        me = self._mentioned_self(message)
        sid = session_label(f"group:{message.group_openid}")
        logger.info("群消息[%s] at=%s: %.80s", self._who(message), "Y" if me else "N",
                    self._message_text(message), extra={"session": sid})
        # passive_ok 固定 False：全量事件的 event_id 不能用于被动回复（40034027），只能主动发送
        await self._route(message, is_at=me, passive_ok=False)

    async def _route(self, message, is_at: bool = False, passive_ok: bool = False) -> None:
        msg_id = getattr(message, "id", None)
        if not msg_id or msg_id in self._seen:
            logger.debug("重复/空消息忽略 id=%s", msg_id)
            return
        self._seen.add(msg_id)
        if len(self._seen) > self._max_seen:
            self._seen = set(list(self._seen)[-self._max_seen // 2 :])
        inbound = await self._normalize(message, is_at=is_at, passive_ok=passive_ok)
        if inbound is None:
            logger.warning("消息归一化失败，跳过 id=%s", msg_id)
            return
        if self.presence:
            await self.presence.handle_message(inbound)
        else:
            logger.warning("presence 未注入，消息跳过 id=%s", msg_id)

    # ---------- 归一化 ----------

    async def _normalize(self, message, is_at: bool = False, passive_ok: bool | None = None) -> dict | None:
        is_group = isinstance(message, GroupMessage)
        if is_group:
            key = f"group:{message.group_openid}"
            target = message.group_openid
            sender = getattr(message.author, "member_openid", None) or "unknown"
            scene = "group"
        else:
            openid = getattr(message.author, "user_openid", None)
            if not openid:
                return None
            key = f"c2c:{openid}"
            target = openid
            sender = openid
            scene = "c2c"
        content = self._message_text(message)
        attachments = await self._download_attachments(message.attachments or [], key=key)
        # is_at：事件来源（group_at/c2c=True）优先；全量消息用 mentions / content <@> 兜底匹配 bot_openid
        # passive_ok：只有 @事件/C2C 事件的消息可用 msg_id+event_id 被动回复；
        #   全量事件（GROUP_MESSAGE_CREATE）的 event_id 平台拒绝用于被动回复（40034027），只能主动
        is_at = bool(is_at or self._mentioned_self(message))
        if passive_ok is None:
            passive_ok = is_at
        return {
            "msg_id": message.id,
            "msg_seq": getattr(message, "msg_seq", None),
            "event_id": getattr(message, "event_id", None),
            "key": key,
            "scene": scene,
            "target": target,
            "sender": sender,
            "sender_name": (getattr(message, "username", "") or "").strip() or None,
            "content": content,
            "attachments": attachments,
            "is_at": is_at,
            "passive_ok": bool(passive_ok),
            "timestamp": message.timestamp,
        }

    # ---------- 辅助 ----------

    @staticmethod
    def _who(message) -> str:
        """日志里的发送者：昵称/8 位 openid（没有昵称时只显示 openid）。"""
        uid = oid8(getattr(message.author, "member_openid", None)
                   or getattr(message.author, "user_openid", None))
        name = (getattr(message, "username", "") or "").strip()
        return f"{name}/{uid}" if name and uid else (name or uid or "unknown")

    @staticmethod
    def _message_text(message) -> str:
        """清洗正文并附富媒体标记；引用消息前置 [引用 被引用内容]。"""
        text = clean_content(message.content)
        media = attachment_summary(getattr(message, "attachments", None))
        if media:
            text = f"{text} {media}".strip()
        quote = (getattr(message, "quote_text", "") or "").strip()
        return f"[引用 {quote}] {text}" if quote else text

    def _mentioned_self(self, message) -> bool:
        """全量消息识别"被@本机器人"。

        QQ 群消息 @ 人/机器人时 content 内嵌 <@openid>（32 位 hex），mentions 亦可能
        携带被@对象。优先精确匹配 config.qq.bot_openid；未学到（平台未订阅
        GROUP_AT_MESSAGE_CREATE）时，从第一条含 <@> 的群消息自动学习 bot_openid
        （测试群机器人唯一，@任何对象即视为@本机），写入 config 后切回精确匹配。
        """
        own = str((self.cfg.qq or {}).get("bot_openid") or "").strip().lower()
        content = (message.content or "") or ""
        ids = set()
        for m in getattr(message, "mentions", None) or []:
            mid = str(getattr(m, "id", "") or "").strip().lower()
            if mid:
                ids.add(mid)
        for match in AT_RE.findall(content):
            ids.add(match.lower())
        if not ids:
            return False
        if own:
            return own in ids
        if AT_RE.search(content):
            for mid in ids:
                self.cfg.set_by_path("bot_openid", mid)
                self.cfg.save()
                logger.info("已学习本机器人 openid=%s（群消息 <@> 识别，写入 config bot_openid）", oid8(mid))
                return True
        return False

    def _learn_bot_openid(self, message) -> None:
        """从第一条 GROUP_AT_MESSAGE_CREATE 事件学习本机器人 openid（mentions[0]=自己），
        写入 config.json，作为全量消息@识别的兜底。"""
        if (self.cfg.qq or {}).get("bot_openid"):
            return
        try:
            mid = str(getattr((getattr(message, "mentions", None) or [None])[0], "id", "") or "").strip()
            if not mid or not mid.lower().startswith("robot"):
                return
            self.cfg.set_by_path("bot_openid", mid)
            self.cfg.save()
            logger.info("已学习本机器人 openid=%s（写入 config bot_openid）", oid8(mid))
        except Exception as e:  # noqa: BLE001
            logger.warning("学习 bot_openid 失败: %s", e)

    async def _download_attachments(self, attachments, key: str | None = None) -> list[dict]:
        """富媒体落地：按类型下载到 temp/<子目录>（emoji/voice/video/other），不平铺。

        - storage.download_video=false 时，视频附件不下载不处理，仅保留 url/元信息供上下文引用
        - temp 文件由 cleanup_temp() 按 storage.temp_ttl_hours 定期清理
        """
        sid = session_label(key)
        download_video = bool((self.cfg.storage_cfg or {}).get("download_video", False))
        out = []
        for a in attachments:
            ctype = a.content_type or ""
            rec = {
                "content_type": ctype,
                "filename": a.filename,
                "url": a.url,
                "size": a.size,
                "local_path": None,
            }
            is_video = "video" in ctype.lower()
            if a.url and not (is_video and not download_video):
                try:
                    fname = f"{int(time.time() * 1000)}_{Path(a.filename or 'file').name}"
                    sub = self._media_kind(ctype)
                    data = await self._http_get(a.url)
                    p = self.cfg.media_dir(sub) / fname
                    p.write_bytes(data)
                    rec["local_path"] = str(p)
                    logger.info("富媒体落地 %s (%d bytes)", p.name, len(data), extra={"session": sid})
                except Exception as e:  # noqa: BLE001
                    logger.warning("富媒体下载失败 %s: %s", a.filename, e, extra={"session": sid})
            elif is_video and not download_video:
                logger.debug("视频不下载（download_video=false），保留 URL: %s", a.filename,
                             extra={"session": sid})
            out.append(rec)
        return out

    @staticmethod
    def _media_kind(ctype: str) -> str:
        """按 content_type 归类：表情包/图片→emoji，语音→voice，视频→video，其余→other。"""
        c = (ctype or "").lower()
        if any(k in c for k in ("voice", "audio", "silk")):
            return "voice"
        if any(k in c for k in ("video",)):
            return "video"
        if any(k in c for k in ("image", "emoji", "face", "gif", "png", "jpeg", "jpg", "webp")):
            return "emoji"
        return "other"

    def cleanup_temp(self) -> int:
        """清理 temp/ 下超过 storage.temp_ttl_hours 小时的文件（递归，保留 logs/），返回移除数。"""
        ttl_hours = float((self.cfg.storage_cfg or {}).get("temp_ttl_hours", 24))
        cutoff = time.time() - ttl_hours * 3600
        removed = 0
        if not self.temp_dir.exists():
            return 0
        for p in self.temp_dir.rglob("*"):
            if not p.is_file():
                continue
            if "logs" in p.parts:
                continue  # 日志不属于媒体 TTL 清理
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("temp 清理: 移除 %d 个过期文件（>%.0fh）", removed, ttl_hours)
        return removed

    @staticmethod
    async def _http_get(url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                return await r.read()

    # ---------- 发送 ----------

    async def send_text(self, target: str, content: str, scene: str, msg_id: str | None = None,
                        msg_seq: int = 1, event_id: str | None = None):
        """发送文本。携带 msg_id 为被动回复（msg_seq 递增支持同一消息多次回复，event_id 为前置事件 ID）。"""
        if scene == "group":
            return await self.api.post_group_message(
                group_openid=target, msg_type=0, content=content,
                msg_id=msg_id, msg_seq=msg_seq, event_id=event_id
            )
        return await self.api.post_c2c_message(
            openid=target, msg_type=0, content=content,
            msg_id=msg_id, msg_seq=msg_seq, event_id=event_id
        )

    async def upload_and_send_media(
        self, target: str, scene: str, file_type: int, source: str,
        msg_id: str | None = None, msg_seq: int = 1,
    ) -> tuple[bool, str]:
        """富媒体上传并发送。file_type: 1 图片 / 2 视频 / 3 语音(silk) / 4 文件。

        source 支持公网 URL，或 temp/tool 下的本地文件。本地文件直接以 base64
        file_data 上传（QQ 官方接口支持，无需公网地址）；超过 10MB 的文件才退回
        storage.public_base_url 签名链接，避免请求体过大。
        返回 (ok, detail)，detail 只含安全的展示名，不含签名 URL。
        """
        local: Path | None = None
        url: str | None = None
        display = "公网素材"
        if source.startswith(("http://", "https://")):
            url = source
        else:
            p = Path(source)
            if not p.is_absolute():
                p = self.cfg.temp_dir / p
            if not p.is_file():
                return False, f"本地文件不存在: {Path(source).name}"
            local, display = p, p.name
            if p.stat().st_size > MAX_INLINE_UPLOAD:
                from ggbot.util.media import public_media_url

                local = None
                url = public_media_url(self.cfg, p)
                if not url:
                    return False, (
                        f"文件超过 {MAX_INLINE_UPLOAD // 1048576}MB，"
                        "需要配置 storage.public_base_url 才能发送"
                    )
        try:
            media = await self._upload_media(target, scene, file_type, url=url,
                                             local=local, name=display)
            if not media.get("file_info"):
                return False, f"上传未返回 file_info: {media}"
            if scene == "group":
                await self.api.post_group_message(
                    group_openid=target, msg_type=7, media=media, msg_id=msg_id, msg_seq=msg_seq
                )
            else:
                await self.api.post_c2c_message(
                    openid=target, msg_type=7, media=media, msg_id=msg_id, msg_seq=msg_seq
                )
            logger.debug("富媒体发送成功 file_type=%d %s", file_type, display,
                         extra={"session": session_label(f"{scene}:{target}")})
            return True, display
        except Exception as e:  # noqa: BLE001
            logger.exception("富媒体发送失败", extra={"session": session_label(f"{scene}:{target}")})
            return False, f"富媒体发送失败: {e}"

    async def _upload_media(self, target: str, scene: str, file_type: int, *,
                            url: str | None, local: Path | None, name: str) -> dict:
        """上传富媒体到 QQ，返回 media 对象（file_uuid/file_info/ttl）。"""
        payload: dict = {"file_type": file_type, "srv_send_msg": False}
        if local is not None:
            payload["file_data"] = base64.b64encode(local.read_bytes()).decode()
            payload["file_name"] = name
        else:
            payload["url"] = url
        if scene == "group":
            payload["group_openid"] = target
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=target)
        else:
            payload["openid"] = target
            route = Route("POST", "/v2/users/{openid}/files", openid=target)
        result = await self.api._http.request(route, json=payload)
        if not isinstance(result, dict):
            raise RuntimeError(f"上传接口返回异常: {result}")
        return {
            "file_uuid": result.get("file_uuid", ""),
            "file_info": result.get("file_info"),
            "ttl": result.get("ttl", 0),
        }
