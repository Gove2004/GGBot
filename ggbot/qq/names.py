"""QQ 开放平台名称解析：openid → 可读名称（群名 / 成员昵称 / 用户昵称）。

QQ 群/用户 openid 是 32 位十六进制串，日志里不可读。本模块调用官方 v2 API
（api.sgroup.qq.com）把 openid 解析成名称，带内存缓存；接口无权限或失败时
静默降级返回 None（调用方回退显示短 id），不影响主流程。

- token：POST bots.qq.com/app/getAppAccessToken（appId+clientSecret），
  沙箱 token 有效期短，缓存并提前刷新
- 群信息：GET /v2/groups/{group_openid}        → {"name": ...}
- 群成员：GET /v2/groups/{group_openid}/members/{member_openid} → {"nickname": ...}
- 用户：  GET /v2/users/{openid}               → {"nickname": ...}
"""

from __future__ import annotations

import logging
import time

import aiohttp

logger = logging.getLogger("ggbot.names")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com/v2"


class NameResolver:
    """openid → 可读名称。线程安全（事件循环内调用即可）；失败静默降级。"""

    def __init__(self, appid: str, secret: str):
        self._appid = appid
        self._secret = secret
        self._token = ""
        self._token_exp = 0.0
        self._groups: dict[str, str] = {}    # group_openid -> name
        self._members: dict[str, str] = {}   # "gid:uid" -> nickname
        self._users: dict[str, str] = {}     # user_openid -> nickname
        self._miss: set[str] = set()         # 解析失败过的 key，短期不再重试
        self._miss_until: dict[str, float] = {}

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
                async with s.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        if r.status in (401, 403, 404, 400):
                            self._miss.add(key)
                            self._miss_until[key] = now + 3600  # 权限/不存在，1h 内不再试
                            logger.debug("名称解析 %s status=%s（降级显示 id）", path, r.status)
                        return None
                    d = await r.json()
            if "name" in d:
                return d.get("name")
            return d.get("nickname") or d.get("name")
        except Exception as e:  # noqa: BLE001
            self._miss.add(key)
            self._miss_until[key] = now + 300
            logger.debug("名称解析异常 %s: %s", path, e)
            return None

    async def group_name(self, gid: str) -> str | None:
        if gid in self._groups:
            return self._groups[gid]
        v = await self._get_json(f"/groups/{gid}", f"g:{gid}")
        if v:
            self._groups[gid] = v
        return v

    async def member_name(self, gid: str, uid: str) -> str | None:
        key = f"m:{gid}:{uid}"
        if key in self._members:
            return self._members[key]
        v = await self._get_json(f"/groups/{gid}/members/{uid}", key)
        if v:
            self._members[key] = v
        return v

    async def user_name(self, uid: str) -> str | None:
        if uid in self._users:
            return self._users[uid]
        v = await self._get_json(f"/users/{uid}", f"u:{uid}")
        if v:
            self._users[uid] = v
        return v
