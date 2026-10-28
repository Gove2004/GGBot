"""QQ 名称解析：openid → 可读名称（群名 / 成员昵称 / 用户昵称）。

QQ 群/用户 openid 是 32 位十六进制串，日志里不可读。解析顺序：
1. 本地名称表（config.names.groups / config.names.users，纯配置驱动、可控）
2. 官方 v2 API（api.sgroup.qq.com，带内存缓存；测试 app 可能无权限，失败降级显示 id）

- token：POST bots.qq.com/app/getAppAccessToken（appId+clientSecret）
- 群信息：GET /v2/groups/{group_openid}/info        → {"group_name": ...}（注意必须带 /info，字段名是 group_name）
- 群成员：GET /v2/groups/{group_openid}/members/{member_openid} → {"nickname": ...}
- 用户：  GET /v2/users/{openid}               → {"nickname": ...}

权限事实（官方文档 2026-10）：群信息/群成员接口为白名单制（11253"仅白名单机器人可用，
请联系平台运营申请权限"），且官方 FAQ 注明为"内邀接口暂未开放申请"；事件 payload 不携带
昵称。故本地 config.names 名称表是当前唯一可靠途径，API 仅作尝试（修通后自动生效）。
"""

from __future__ import annotations

import logging
import time

import aiohttp

logger = logging.getLogger("ggbot.names")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# 官方统一地址 api.bot.qq.com（botpy 内部发消息走 api.sgroup.qq.com；群信息等接口文档指向 api.bot.qq.com）
API_BASE = "https://api.bot.qq.com/v2"


class NameResolver:
    """openid → 可读名称。本地表优先，API 补充；失败静默降级（回退显示短 id）。"""

    def __init__(self, appid: str, secret: str, local: dict | None = None):
        self._appid = appid
        self._secret = secret
        self._local_groups: dict[str, str] = (local or {}).get("groups", {}) or {}
        self._local_users: dict[str, str] = (local or {}).get("users", {}) or {}
        self._token = ""
        self._token_exp = 0.0
        self._groups: dict[str, str] = {}    # group_openid -> name（API 缓存）
        self._members: dict[str, str] = {}   # "gid:uid" -> nickname
        self._users: dict[str, str] = {}     # user_openid -> nickname
        self._miss: set[str] = set()         # 解析失败过的 key，短期不再重试
        self._miss_until: dict[str, float] = {}
        self._diag_done: set[str] = set()    # 首次失败已诊断打印（避免刷屏）

    async def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and self._token_exp > now + 60:
            return self._token
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(TOKEN_URL, json={
                    "appId": self._appid, "clientSecret": self._secret,
                }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        logger.warning("获取 QQ token 失败 status=%s", r.status)
                        return ""
                    d = await r.json()
            self._token = d.get("access_token", "")
            self._token_exp = now + int(d.get("expires_in", 3600)) * 0.8
            if not self._token:
                logger.warning("QQ token 响应无 access_token: %s", d)
            return self._token
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 QQ token 异常: %s", e)
            return ""

    async def _get_json(self, path: str, key: str) -> str | None:
        now = time.monotonic()
        if key in self._miss and now < self._miss_until.get(key, 0):
            return None
        token = await self._get_token()
        if not token:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                # 鉴权格式必须为 "QQBot {access_token}"（Bearer 会返回 11241 格式错误）
                async with s.get(f"{API_BASE}{path}", headers={"Authorization": f"QQBot {token}"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        if key not in self._diag_done:
                            self._diag_done.add(key)
                            body = (await r.text())[:200]
                            logger.warning("名称解析失败 %s status=%s body=%.200s（如需真实名称：开放平台开通权限，或在 config.names 配置本地名称表）",
                                           path, r.status, body)
                        if r.status in (401, 403, 404, 400):
                            self._miss.add(key)
                            self._miss_until[key] = now + 3600  # 权限/不存在，1h 内不再试
                        return None
                    d = await r.json()
            # 群信息 → group_name；群成员/用户 → nickname（文档字段名，勿用 name）
            return d.get("group_name") or d.get("nickname") or d.get("name")
        except Exception as e:  # noqa: BLE001
            self._miss.add(key)
            self._miss_until[key] = now + 300
            logger.debug("名称解析异常 %s: %s", path, e)
            return None

    async def group_name(self, gid: str) -> str | None:
        if gid in self._local_groups:
            return self._local_groups[gid]
        if gid in self._groups:
            return self._groups[gid]
        v = await self._get_json(f"/groups/{gid}/info", f"g:{gid}")
        if v:
            self._groups[gid] = v
        return v

    async def member_name(self, gid: str, uid: str) -> str | None:
        # 成员昵称：本地 users 表（用户 openid 按 app 维度唯一）优先，再 API
        if uid in self._local_users:
            return self._local_users[uid]
        key = f"m:{gid}:{uid}"
        if key in self._members:
            return self._members[key]
        v = await self._get_json(f"/groups/{gid}/members/{uid}", key)
        if v:
            self._members[key] = v
        return v

    async def user_name(self, uid: str) -> str | None:
        if uid in self._local_users:
            return self._local_users[uid]
        if uid in self._users:
            return self._users[uid]
        v = await self._get_json(f"/users/{uid}", f"u:{uid}")
        if v:
            self._users[uid] = v
        return v
