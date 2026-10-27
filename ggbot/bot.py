"""QQ 消息收发网关：qq-botpy 事件接收、归一化、去重、富媒体落地、发送能力。

- 事件：群@（on_group_at_message_create）、单聊（on_c2c_message_create）
- 富媒体附件（图片/语音/视频/文件）下载到 Temp/ 并记录公网 URL（可转发）
- 发送：文本被动回复（msg_id + 递增 msg_seq）、富媒体上传（post_group_file → file_info → msg_type=7）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiohttp
import botpy
from botpy.message import C2CMessage, GroupMessage

logger = logging.getLogger("ggbot.bot")


class QQBot(botpy.Client):
    def __init__(self, config, presence=None, intents=None):
        super().__init__(intents=intents, timeout=30)
        self.cfg = config
        self.presence = presence
        self.temp_dir: Path = config.temp_dir
        self._seen: set[str] = set()
        self._max_seen = 5000

    # ---------- 事件 ----------

    async def on_ready(self) -> None:
        logger.info("QQBot 已连接 WebSocket gateway")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        await self._route(message)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        await self._route(message)

    async def on_group_add_robot(self, message) -> None:
        logger.info("机器人加入群聊: %s", getattr(message, "group_openid", None))

    async def _route(self, message) -> None:
        msg_id = getattr(message, "id", None)
        if not msg_id or msg_id in self._seen:
            logger.debug("重复/空消息忽略: id=%s", msg_id)
            return
        self._seen.add(msg_id)
        if len(self._seen) > self._max_seen:
            self._seen = set(list(self._seen)[-self._max_seen // 2 :])
        inbound = await self._normalize(message)
        if inbound is None:
            logger.warning("消息归一化失败，跳过: id=%s", msg_id)
            return
        logger.debug(
            "收到消息 scene=%s target=%s sender=%s at=%s content=%.60r attachments=%d",
            inbound["scene"], inbound["target"], inbound["sender"],
            inbound["is_at"], inbound["content"], len(inbound["attachments"] or []),
        )
        if self.presence:
            await self.presence.handle_message(inbound)
        else:
            logger.info("presence 未注入，消息跳过: %s", msg_id)

    # ---------- 归一化 ----------

    async def _normalize(self, message) -> dict | None:
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
        attachments = await self._download_attachments(message.attachments or [])
        is_at = bool(getattr(message, "mentions", None)) or "@" in content[:10]
        return {
            "msg_id": message.id,
            "key": key,
            "scene": scene,
            "target": target,
            "sender": sender,
            "content": content,
            "attachments": attachments,
            "is_at": is_at,
            "timestamp": message.timestamp,
        }

    async def _download_attachments(self, attachments) -> list[dict]:
        """富媒体落地：下载到 Temp/，同时保留公网 URL（可转发）。"""
        out = []
        for a in attachments:
            rec = {
                "content_type": a.content_type,
                "filename": a.filename,
                "url": a.url,
                "size": a.size,
                "local_path": None,
            }
            if a.url:
                try:
                    fname = f"{int(time.time() * 1000)}_{Path(a.filename or 'file').name}"
                    data = await self._http_get(a.url)
                    p = self.temp_dir / fname
                    p.write_bytes(data)
                    rec["local_path"] = str(p)
                    logger.info("富媒体落地: %s (%d bytes)", p, len(data))
                except Exception as e:  # noqa: BLE001
                    logger.warning("富媒体下载失败: %s", e)
            out.append(rec)
        return out

    @staticmethod
    async def _http_get(url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                return await r.read()

    # ---------- 发送 ----------

    async def send_text(self, target: str, content: str, scene: str, msg_id: str | None = None, msg_seq: int = 1):
        """发送文本。携带 msg_id 为被动回复（msg_seq 递增支持同一消息多次回复）。"""
        logger.debug("send_text scene=%s target=%s msg_id=%s msg_seq=%d content=%.60r",
                     scene, target, msg_id, msg_seq, content)
        if scene == "group":
            return await self.api.post_group_message(
                group_openid=target, msg_type=0, content=content, msg_id=msg_id, msg_seq=msg_seq
            )
        return await self.api.post_c2c_message(
            openid=target, msg_type=0, content=content, msg_id=msg_id, msg_seq=msg_seq
        )

    async def upload_and_send_media(
        self, target: str, scene: str, file_type: int, source: str,
        msg_id: str | None = None, msg_seq: int = 1,
    ) -> tuple[bool, str]:
        """富媒体上传并发送。file_type: 1 图片 / 2 视频 / 3 语音 / 4 文件。

        source 支持公网 URL（直接上传）或本地路径（需配置 storage.public_base_url）。
        返回 (ok, detail)。
        """
        url = source
        if not url.startswith(("http://", "https://")):
            base = (self.cfg.storage_cfg or {}).get("public_base_url") or ""
            if not base:
                return False, (
                    "本地文件无法直接上传：需要公网 URL。请传群友消息里附带的 URL，"
                    "或配置 storage.public_base_url 指向该文件所在公网目录。"
                )
            url = base.rstrip("/") + "/" + Path(source).name
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
            logger.debug("富媒体发送成功 file_type=%d url=%.80s", file_type, url)
            return True, url
        except Exception as e:  # noqa: BLE001
            logger.exception("富媒体发送失败")
            return False, f"富媒体发送失败: {e}"
