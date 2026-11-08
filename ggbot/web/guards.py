"""面板的安全层与把手：令牌/Cookie 鉴权、按 IP 的限流、跨站拦截、密钥脱敏。

路由不再自己写鉴权：受保护的 router 统一挂 `Depends(authenticated)`，加一条路由就自动带上门禁，
不存在「新接口忘了 _auth」这种漏法。
登录那三条是本模块自己的 router，它们必须在门禁之外（拿到 Cookie 之前不可能通过鉴权）。

Panel 还是面板伸进运行中对象的唯一一只把手：工具注册表、表情库、任务、权限模块都从这里取，
路由不 direct reach 进 presence 的内部结构。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ggbot.core.config import BASE_DIR, SECRET_KEYS

logger = logging.getLogger("ggbot.web")

COOKIE_NAME = "ggbot_panel"
SESSION_MAX_AGE = 7 * 24 * 3600
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW_SEC = 60
API_RATE_LIMIT = 240
API_RATE_WINDOW_SEC = 10
RATE_MAP_MAX = 2048

# 安全响应头（无内联脚本/样式，前端全部走 /static）
CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def mask_secrets(node):
    """递归脱敏：密钥字段显示为 ***。"""
    if isinstance(node, dict):
        return {k: ("***" if k in SECRET_KEYS else mask_secrets(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [mask_secrets(v) for v in node]
    return node


def _session_value(token: str) -> str:
    """Cookie 值：令牌的 HMAC，浏览器端不持有原始令牌。"""
    return hmac.new(token.encode("utf-8"), b"ggbot-panel-v1", hashlib.sha256).hexdigest()


def _same_origin(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    try:
        return urlparse(origin).netloc == (host or "")
    except ValueError:
        return False


def _rel(path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return resolved.as_posix()


class Panel:
    """一次 create_app 一份：配置里的 web 段、限流账本，以及通往运行中对象的把手。"""

    def __init__(self, cfg, storage, memory, presence):
        self.cfg = cfg
        self.storage = storage
        self.memory = memory
        self.presence = presence
        web = cfg.web_cfg or {}
        self.token = str(web.get("token") or "")
        self.cookie_secure = bool(web.get("cookie_secure", False))
        self.trust_proxy = bool(web.get("trust_proxy", False))
        self.login_fails: dict[str, list[float]] = {}
        self.api_hits: dict[str, list[float]] = {}

    # ---------- 鉴权 ----------

    def authenticate(self, request: Request) -> None:
        if not self.token:
            return
        x_token = request.headers.get("x-token")
        if x_token and secrets.compare_digest(x_token.encode("utf-8"),
                                             self.token.encode("utf-8")):
            return
        got = request.cookies.get(COOKIE_NAME, "")
        if got and secrets.compare_digest(got.encode("utf-8"),
                                         _session_value(self.token).encode("utf-8")):
            return
        raise HTTPException(status_code=401, detail="unauthorized")

    def cookie_ok(self, cookies) -> bool:
        if not self.token:
            return True
        got = cookies.get(COOKIE_NAME, "")
        return bool(got) and secrets.compare_digest(
            got.encode("utf-8"), _session_value(self.token).encode("utf-8")
        )

    # ---------- 限流 ----------

    def client_ip(self, request: Request) -> str:
        # 仅在明确启用反代时信任 X-Forwarded-For 的首个地址，避免直连时被伪造绕过限流
        if self.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
        return request.client.host if request.client else "unknown"

    def rate_limited(self, ip: str) -> int:
        """记录一次 API 请求；超过窗口上限时返回需等待的秒数。"""
        now = time.monotonic()
        hits = [t for t in self.api_hits.get(ip, []) if now - t < API_RATE_WINDOW_SEC]
        if len(hits) >= API_RATE_LIMIT:
            self.api_hits[ip] = hits
            return max(1, int(API_RATE_WINDOW_SEC - (now - hits[0])) + 1)
        hits.append(now)
        self.api_hits[ip] = hits
        if len(self.api_hits) > RATE_MAP_MAX:
            cutoff = now - API_RATE_WINDOW_SEC
            stale = [k for k, v in self.api_hits.items() if not v or v[-1] < cutoff]
            for key in stale:
                self.api_hits.pop(key, None)
            if len(self.api_hits) > RATE_MAP_MAX:
                overflow = len(self.api_hits) - RATE_MAP_MAX
                for key in sorted(self.api_hits, key=lambda k: self.api_hits[k][-1])[:overflow]:
                    self.api_hits.pop(key, None)
        return 0

    def login_guard(self, ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in self.login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
        self.login_fails[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILS:
            retry = max(1, int(LOGIN_WINDOW_SEC - (now - recent[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"尝试过于频繁，请 {retry} 秒后再试",
                headers={"Retry-After": str(retry)},
            )

    def login_failed(self, ip: str) -> None:
        self.login_fails.setdefault(ip, []).append(time.monotonic())

    # ---------- 运行环境摘要 ----------

    def env(self) -> dict:
        """面板「运行环境」摘要：模型名、服务域名与相对路径，不含任何密钥。"""
        cfg = self.cfg
        vision = cfg.vision
        embedding = cfg.embedding
        return {
            "chat_model": cfg.model or "-",
            "chat_host": urlparse(str(cfg.llm.get("base_url") or "")).netloc or "-",
            "vision_model": vision["model"] if vision["enabled"] else "",
            "vision_host": urlparse(str(vision["base_url"] or "")).netloc or "",
            "memory_model": embedding["model"] if embedding["enabled"] else "",
            "memory_mode": "向量" if embedding["enabled"] else "关键词",
            "log_level": str(cfg.web_cfg.get("log_level", "DEBUG")),
            "db": _rel(cfg.db_path),
            "temp": _rel(cfg.temp_dir),
        }

    # ---------- 通往运行中对象的把手 ----------

    @property
    def executor(self):
        return getattr(self.presence, "executor", None)

    def tool(self, name: str):
        """按名字取一个已挂载的工具；没挂载就 404（工具页的启停与门槛都问它）。"""
        ex = self.executor
        tool = ex.registry.tools.get(name) if ex is not None else None
        if tool is None:
            raise HTTPException(status_code=404, detail="tool not found")
        return tool

    def emoji_lib(self):
        ex = self.executor
        tool = ex.registry.tools.get("send_emoji") if ex is not None else None
        return getattr(tool, "lib", None)

    def emoji_lib_or_503(self):
        lib = self.emoji_lib()
        if lib is None:
            raise HTTPException(503, "表情工具未启用（工具页里打开 send_emoji 才能管理素材）")
        return lib

    def tasks_or_none(self):
        """任务的操作面；工具没挂载就返回 None（配置是 tool/task/config.json）。"""
        rt = getattr(self.executor, "rt", None)
        if rt is None:
            return None
        from tool.task.task import Tasks

        return Tasks(rt)

    def tasks(self):
        tasks = self.tasks_or_none()
        if tasks is None:
            raise HTTPException(503, "任务工具未挂载，暂时改不了任务")
        return tasks

    def perms(self):
        p = getattr(self.presence, "perms", None)
        if p is None:
            raise HTTPException(status_code=503, detail="权限模块未就绪")
        return p


def panel_of(request: Request) -> Panel:
    return request.app.state.panel


async def authenticated(request: Request) -> None:
    """受保护 router 的门禁依赖。"""
    panel_of(request).authenticate(request)


def protected_router() -> APIRouter:
    """带门禁的资源路由都挂在这上面：写新接口不必再自己调一次鉴权。"""
    return APIRouter(dependencies=[Depends(authenticated)])


# ---------- 登录（唯一不设门禁的三条 API） ----------


class LoginIn(BaseModel):
    token: str = ""


open_router = APIRouter()


@open_router.get("/api/auth")
async def auth_state(request: Request):
    return {"authenticated": panel_of(request).cookie_ok(request.cookies),
            "required": bool(panel_of(request).token)}


@open_router.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    p = panel_of(request)
    ip = p.client_ip(request)
    p.login_guard(ip)
    if p.token and not secrets.compare_digest(
        (body.token or "").encode("utf-8"), p.token.encode("utf-8")
    ):
        p.login_failed(ip)
        raise HTTPException(status_code=401, detail="令牌无效")
    p.login_fails.pop(ip, None)
    if p.token:
        response.set_cookie(
            COOKIE_NAME,
            _session_value(p.token),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=p.cookie_secure,
            path="/",
        )
    return {"ok": True}


@open_router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


def same_origin(request: Request) -> bool:
    """中间件用：跨站的写请求一律拦掉。"""
    return _same_origin(request.headers.get("origin"), request.headers.get("host"))
