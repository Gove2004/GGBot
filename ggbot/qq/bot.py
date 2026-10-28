"""QQ 消息收发网关：qq-botpy 事件接收、归一化、去重、富媒体落地、发送能力。

- 事件：群@（on_group_at_message_create）、单聊（on_c2c_message_create）、群全量（on_group_message_create）
- 富媒体附件（图片/语音/视频/文件）下载到 temp/ 并记录公网 URL（可转发）
- 发送：文本被动回复（msg_id + 递增 msg_seq）、富媒体上传（post_group_file → file_info → msg_type=7）
- 日志：openid 只显示前 8 位（不做名称解析，无白名单/网络依赖）
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import aiohttp
import botpy
from botpy.message import C2CMessage, GroupMessage

from ggbot.util.session import oid8, session_label

logger = logging.getLogger("ggbot.bot")

# QQ 群消息 @ 人/机器人：content 内嵌 <@openid>（openid 为 32 位十六进制）
AT_RE = re.compile(r"<@([0-9A-Fa-f]{32})>")


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
        self._register_group_message_parser()
        logger.info("QQBot就绪: WebSocket gateway 已连接")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        # GROUP_AT_MESSAGE_CREATE：只有真正被@本机器人时触发（事件通道）
        self._learn_bot_openid(message)
        sid = session_label(f"group:{message.group_openid}")
        sender = oid8(getattr(message.author, "member_openid", None))
        logger.info("群@ sender=%s: %.50s", sender, (message.content or "").strip(),
                    extra={"session": sid})
        await self._route(message, is_at=True, passive_ok=True)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        # 私聊 = 直接找机器人对话，等价于 @；C2C 事件可被动回复
        openid = getattr(message, "author", None) and getattr(message.author, "user_openid", None)
        sid = session_label(f"c2c:{openid}")
        logger.info("私聊 sender=%s: %.50s", oid8(openid), (message.content or "").strip(),
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

    def _register_group_message_parser(self) -> None:
        """注册群全量消息事件（GROUP_MESSAGE_CREATE，私域群全量消息）。

        botpy 1.2.1 未内置该事件 parser（收到即报 unknown event 并丢弃），
        这里在运行时向 ConnectionState.parsers 注入同名解析器，使普通（非@）群消息进入统一路由。
        """
        try:
            state = self._connection.state
            if "group_message_create" in state.parsers:
                return
            import types

            def parse_group_message_create(self, payload):
                _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
                self._dispatch("group_message_create", _message)

            state.parse_group_message_create = types.MethodType(parse_group_message_create, state)
            state.parsers["group_message_create"] = state.parse_group_message_create
            logger.debug("已注册 group_message_create（群全量消息）事件解析")
        except Exception as e:  # noqa: BLE001
            logger.warning("注册 group_message_create 解析失败: %s", e)

    async def on_group_message_create(self, message: GroupMessage) -> None:
        """群全量消息（私域权限，所有普通消息；含@别人的消息，不含对本机器人的@）。

        is_at 判定：全量消息的 mentions / content <@openid> 识别是否@本机器人——
        优先精确匹配 config.qq.bot_openid；未学到时从第一条含 <@> 的消息自动学习
        （测试群机器人唯一），之后切回精确匹配。"""
        me = self._mentioned_self(message)
        sid = session_label(f"group:{message.group_openid}")
        sender = oid8(getattr(message.author, "member_openid", None))
        logger.info("群消息 sender=%s at=%s: %.50s", sender, "Y" if me else "N",
                    (message.content or "").strip(), extra={"session": sid})
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
        sid = session_label(inbound["key"])
        logger.debug(
            "归一化 scene=%s target=%s sender=%s at=%s passive=%s att=%d content=%.60r",
            inbound["scene"], oid8(inbound["target"]), oid8(inbound["sender"]),
            inbound["is_at"], inbound["passive_ok"], len(inbound["attachments"] or []),
            inbound["content"], extra={"session": sid},
        )
        if self.presence:
            await self.presence.handle_message(inbound)
        else:
            logger.info("presence 未注入，消息跳过 id=%s", msg_id, extra={"session": sid})

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
            sender = "c2c"
            scene = "c2c"
        content = (message.content or "").strip()
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
            "content": content,
            "attachments": attachments,
            "is_at": is_at,
            "passive_ok": bool(passive_ok),
            "timestamp": message.timestamp,
        }

    # ---------- 辅助 ----------

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
        logger.debug("send_text target=%s msg_id=%s seq=%d content=%.60r",
                     oid8(target), msg_id, msg_seq, content,
                     extra={"session": session_label(f"{scene}:{target}")})
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
        """富媒体上传并发送。file_type: 1 图片 / 2 视频 / 3 语音 / 4 文件。

        source 支持公网 URL，或 temp/db-tool 下的本地文件（需配置
        storage.public_base_url 指向 Web 面板公网地址，本地文件经签名 /media URL 暴露）。
        返回 (ok, detail)，detail 只含安全的展示名，不含签名 URL。
        """
        from ggbot.util.media import public_media_url

        url = source
        display = "公网素材"
        if not url.startswith(("http://", "https://")):
            p = Path(source)
            if not p.is_absolute():
                p = self.cfg.temp_dir / p
            if not p.is_file():
                return False, f"本地文件不存在: {Path(source).name}"
            url = public_media_url(self.cfg, p) or ""
            display = p.name
            if not url:
                return False, (
                    "本地文件无法上传：需要公网 URL。请配置 storage.public_base_url "
                    "指向本 Web 面板的公网地址（本地文件会生成带签名的 /media 临时链接）。"
                )
        try:
            if scene == "group":
                media = await self.api.post_group_file(
                    group_openid=target, file_type=file_type, url=url
                )
            else:
                media = await self.api.post_c2c_file(
                    openid=target, file_type=file_type, url=url
                )
            file_info = media.get("file_info") if isinstance(media, dict) else getattr(media, "file_info", None)
            if not file_info:
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
