"""Web 控制面板：FastAPI + WebSocket + 静态前端（ggbot/web/static）。

前端与后端分离：ggbot/web/static/{index.html,style.css,app.js}，经 /static 托管，可独立修改热加载。
功能：在线状态、会话与上下文、消息流、记忆（检索/写入/编辑/整理）、规则、配置热更新、日志查看。

鉴权（安全）：
- 登录：POST /api/login 校验 config.web.token，成功后写入 HttpOnly + SameSite=Strict Cookie，
  浏览器不再把令牌放进 URL 或 localStorage；
- API：Cookie 或 X-Token 头（CLI/脚本），常量时间比较；/ws 同样校验 Cookie；
- 失败限流、跨站 Origin 拦截、CSP 等安全响应头；令牌为空时仅建议本机使用。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ggbot.util.session import SYSTEM_LABEL, session_label

logger = logging.getLogger("ggbot.web")

WEB_DIR = Path(__file__).resolve().parent / "static"

# 四列日志：时间 | 等级 | 会话 | 内容（等级/会话按宽度右填充空格）
LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+)\s*\| ((?:群聊|私聊|系统)[0-9A-Za-z]{8})\s*\| (.*)$"
)
# 旧版五列（时间 | 等级 | 位置 | 会话 | 内容）与三列（时间 | 等级 | 位置 | 内容）兜底解析
OLD5_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+)\s*\| ([\w.]+)\s*\| (.*?)\s*\| (.*)$"
)
OLD3_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+)\s*\| ([\w.]+)\s*\| (.*)$"
)

COOKIE_NAME = "ggbot_panel"
SESSION_MAX_AGE = 7 * 24 * 3600
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW_SEC = 60

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


class LoginIn(BaseModel):
    token: str = ""


class RuleIn(BaseModel):
    content: str
    trigger: str | None = None


class MemoryIn(BaseModel):
    content: str
    importance: int = 1
    session_id: str | None = None


class MemoryEditIn(BaseModel):
    content: str | None = None
    importance: int | None = None


class SettingIn(BaseModel):
    path: str
    value: object


class ToolToggleIn(BaseModel):
    enabled: bool


# 允许通过 Web 热更新的配置路径（其余只读，避免误改关键结构）
ALLOWED_SETTINGS = [
    "deepseek.request.temperature",
    "deepseek.request.top_p",
    "deepseek.request.max_tokens",
    "deepseek.request.thinking",
    "deepseek.request.stream",
    "behavior.presence.online_duration_sec",
    "behavior.presence.offline_interval_sec",
    "behavior.presence.online_duration_jitter",
    "behavior.presence.offline_interval_jitter",
    "behavior.presence.wake_on_at_message",
    "behavior.presence.wake_on_c2c",
    "behavior.presence.renew_on_reply",
    "behavior.presence.renew_per_reply_sec",
    "behavior.presence.renew_max_sec",
    "behavior.presence.renew_jitter",
    "behavior.reply.segment_gap_sec",
    "behavior.reply.debounce_sec",
    "behavior.reply.debounce_max_sec",
    "behavior.reply.read_delay_min_sec",
    "behavior.reply.read_delay_max_sec",
    "behavior.reply.typing_chars_per_sec",
    "behavior.reply.typing_min_sec",
    "behavior.reply.typing_max_sec",
    "behavior.reply.typing_jitter",
    "behavior.backlog.max_scan_messages",
    "web.log_level",
]

SECRET_KEYS = ("api_key", "secret", "appsecret", "token", "auth_code", "password")


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


def _coerce_setting(current, value):
    """按当前配置类型校验/转换；不合法返回 _INVALID。"""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return _INVALID
    if isinstance(current, int):
        if isinstance(value, bool):
            return _INVALID
        try:
            return int(value)
        except (TypeError, ValueError):
            return _INVALID
    if isinstance(current, float):
        if isinstance(value, bool):
            return _INVALID
        try:
            return float(value)
        except (TypeError, ValueError):
            return _INVALID
    if isinstance(current, str):
        return str(value)
    return value


_INVALID = object()


def create_app(cfg, storage, memory, presence) -> FastAPI:
    app = FastAPI(title="GGBot Panel", docs_url=None, redoc_url=None, openapi_url=None)
    token = str((cfg.web_cfg or {}).get("token") or "")
    cookie_secure = bool((cfg.web_cfg or {}).get("cookie_secure", False))
    login_fails: dict[str, list[float]] = {}

    def _auth(request: Request, x_token: str | None) -> None:
        if not token:
            return
        if x_token and secrets.compare_digest(x_token.encode("utf-8"), token.encode("utf-8")):
            return
        got = request.cookies.get(COOKIE_NAME, "")
        if got and secrets.compare_digest(got.encode("utf-8"), _session_value(token).encode("utf-8")):
            return
        raise HTTPException(status_code=401, detail="unauthorized")

    def _cookie_ok(cookies) -> bool:
        if not token:
            return True
        got = cookies.get(COOKIE_NAME, "")
        return bool(got) and secrets.compare_digest(
            got.encode("utf-8"), _session_value(token).encode("utf-8")
        )

    def _login_guard(ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
        login_fails[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILS:
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")

    def _login_failed(ip: str) -> None:
        login_fails.setdefault(ip, []).append(time.monotonic())

    # 安全响应头 + 跨站写请求拦截
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not _same_origin(request.headers.get("origin"), request.headers.get("host")):
                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    # 静态前端：web/index.html + /static/style.css + /static/app.js
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    # ---------- 本地媒体（QQ 富媒体上传用的签名 URL，无需 Cookie） ----------

    @app.get("/media/{exp}/{sig}/{rel:path}", response_class=FileResponse)
    async def media(exp: int, sig: str, rel: str):
        from ggbot.util.media import verify_path

        p = verify_path(cfg, exp, sig, rel)
        if not p:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            p,
            headers={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"},
        )

    # ---------- 登录 ----------

    @app.get("/api/auth")
    async def auth_state(request: Request):
        return {"authenticated": _cookie_ok(request.cookies), "required": bool(token)}

    @app.post("/api/login")
    async def login(body: LoginIn, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        _login_guard(ip)
        if token and not secrets.compare_digest(
            (body.token or "").encode("utf-8"), token.encode("utf-8")
        ):
            _login_failed(ip)
            raise HTTPException(status_code=401, detail="令牌无效")
        login_fails.pop(ip, None)
        if token:
            response.set_cookie(
                COOKIE_NAME,
                _session_value(token),
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="strict",
                secure=cookie_secure,
                path="/",
            )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    # ---------- 状态 ----------

    @app.get("/api/status")
    async def status(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        return presence.status()

    @app.get("/api/stats")
    async def stats(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        out = storage.stats()
        live = presence.status()
        out["online_sessions"] = live.get("online_sessions", 0)
        out["state"] = live.get("state", "offline")
        return out

    @app.get("/api/sessions")
    async def sessions(
        request: Request,
        limit: int = 200,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        rows = storage.list_sessions(min(max(limit, 1), 1000))
        live = {s["sid"]: s for s in (presence.status().get("sessions") or [])}
        for row in rows:
            row["label"] = session_label(row.get("id"))
            st = live.get(row.get("id"))
            row["live"] = st or {"state": "offline", "reason": "idle", "remaining_sec": 0, "idle_sec": None}
        return rows

    @app.get("/api/sessions/{sid}/messages")
    async def session_messages(
        sid: str,
        request: Request,
        limit: int = 100,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        row = storage.get_session(sid)
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        row["label"] = session_label(sid)
        return {
            "session": row,
            "summary": storage.get_summary(sid),
            "messages": storage.get_recent_messages(sid, min(max(limit, 1), 500)),
        }

    @app.delete("/api/sessions/{sid}/context")
    async def session_clear(
        sid: str,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        if not storage.get_session(sid):
            raise HTTPException(status_code=404, detail="session not found")
        if presence is not None:
            await presence.forget_session(sid)
        removed = storage.clear_session_context(sid)
        logger.info("Web 清理会话上下文 %s: %d 条消息", session_label(sid), removed["messages"])
        return {"ok": True, **removed}

    # ---------- 消息 ----------

    @app.get("/api/messages")
    async def messages(
        request: Request,
        limit: int = 30,
        session_id: str | None = None,
        role: str | None = None,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        return storage.recent_messages_global(
            min(max(limit, 1), 500), session_id=session_id or None, role=role or None
        )

    # ---------- 记忆 ----------

    @app.get("/api/memories")
    async def memories(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        session_id: str | None = None,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        sid = session_id or None
        n = min(max(limit, 1), 500)
        return {
            "items": storage.list_memories(sid, n, max(offset, 0)),
            "total": storage.count_memories(sid),
        }

    @app.get("/api/memories/search")
    async def memory_search(
        q: str,
        request: Request,
        k: int = 5,
        session_id: str | None = None,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        return await asyncio.to_thread(
            memory.search, q, min(max(k, 1), 50), session_id or None, None
        )

    @app.post("/api/memories")
    async def memory_add(
        body: MemoryIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        mid = await asyncio.to_thread(
            memory.add, content, "fact", max(1, min(5, body.importance)), body.session_id or None
        )
        return {"ok": True, "memory_id": mid}

    @app.put("/api/memories/{memory_id}")
    async def memory_edit(
        memory_id: str,
        body: MemoryEditIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        if body.content is None and body.importance is None:
            raise HTTPException(status_code=400, detail="没有需要修改的字段")
        try:
            ok = await asyncio.to_thread(
                memory.update,
                memory_id,
                body.content,
                None if body.importance is None else max(1, min(5, body.importance)),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not ok:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True}

    @app.post("/api/memories/tidy")
    async def memory_tidy(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        removed = await asyncio.to_thread(memory.tidy)
        return {"ok": True, "removed": removed}

    @app.delete("/api/memories/{memory_id}")
    async def memory_delete(
        memory_id: str,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        storage.delete_memory(memory_id)
        return {"ok": True}

    # ---------- 规则 ----------

    @app.get("/api/rules")
    async def rules(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        return storage.list_rules(active_only=False)

    @app.post("/api/rules")
    async def rule_add(
        body: RuleIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        rid = storage.add_rule(body.content, body.trigger)
        return {"ok": True, "rule_id": rid}

    @app.post("/api/rules/{rule_id}/toggle")
    async def rule_toggle(
        rule_id: int,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        row = next((r for r in storage.list_rules(active_only=False) if r["id"] == rule_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        storage.set_rule_active(rule_id, not row["active"])
        return {"ok": True}

    @app.delete("/api/rules/{rule_id}")
    async def rule_delete(
        rule_id: int,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        storage.delete_rule(rule_id)
        return {"ok": True}

    # ---------- 工具（插件清单 / 启停） ----------

    @app.get("/api/tools")
    async def list_tools(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        ex = getattr(presence, "executor", None)
        if ex is None:
            return {"tools": []}
        rows = ex.list_tools()
        for row in rows:
            row["config"] = mask_secrets(row.get("config") or {})
        return {"tools": rows}

    @app.post("/api/tools/{name}/toggle")
    async def toggle_tool(
        name: str,
        body: ToolToggleIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        ex = getattr(presence, "executor", None)
        tool = ex.registry.tools.get(name) if ex is not None else None
        if tool is None:
            raise HTTPException(status_code=404, detail="tool not found")
        if not tool.toggleable:
            raise HTTPException(status_code=400, detail="该工具随同组工具启停")
        data = ex.toggle(name, body.enabled)
        if data is None:
            raise HTTPException(status_code=404, detail="tool not found")
        logger.info("Web 工具启停 %s=%s", name, body.enabled)
        return {"ok": True, "config": mask_secrets(data)}

    # ---------- 配置（Web 热更新） ----------

    @app.get("/api/config")
    async def get_config(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        return {"config": mask_secrets(cfg.data), "allowed": ALLOWED_SETTINGS}

    @app.post("/api/config")
    async def update_config(
        body: list[SettingIn],
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        updated: list[str] = []
        rejected: list[str] = []
        for item in body:
            if item.path not in ALLOWED_SETTINGS:
                rejected.append(item.path)
                continue
            value = _coerce_setting(cfg.get_by_path(item.path), item.value)
            if value is _INVALID:
                rejected.append(item.path)
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                rejected.append(item.path)
                continue
            if cfg.set_by_path(item.path, value):
                updated.append(item.path)
        if "web.log_level" in updated:
            from ggbot.util.logging_setup import set_level

            set_level(cfg.data["web"]["log_level"])
        cfg.save()
        logger.info("Web 配置更新: %s（拒绝 %d 项）", updated, len(rejected))
        return {
            "ok": True,
            "updated": updated,
            "rejected": rejected,
            "config": mask_secrets(cfg.data),
        }

    # ---------- 日志查看 ----------

    @app.get("/api/logs")
    async def get_logs(
        request: Request,
        lines: int = 200,
        level: str = "INFO",
        session: str | None = None,
        q: str | None = None,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        from ggbot.util.logging_setup import tail_log

        n = min(max(lines, 1), 2000)
        raw = tail_log(cfg.log_file, n, level)
        logs = []
        for line in raw:
            m = LOG_LINE_RE.match(line)
            if m:
                ts, lv, sess, content = m.groups()
            else:
                m = OLD5_LOG_RE.match(line)
                if m:
                    ts, lv, _mod, sess, content = m.groups()
                else:
                    m = OLD3_LOG_RE.match(line)
                    if not m:
                        logs.append({"ts": "", "level": "-", "session": SYSTEM_LABEL, "content": line})
                        continue
                    ts, lv, _mod, content = m.groups()
                    sess = SYSTEM_LABEL
            if session and session not in sess:
                continue
            if q and q not in content:
                continue
            logs.append({"ts": ts[:23], "level": lv, "session": sess, "content": content})
        try:
            log_name = cfg.log_file.relative_to(cfg.log_file.parent.parent.parent).as_posix()
        except ValueError:
            log_name = cfg.log_file.name
        return {"level": level, "file": log_name, "logs": logs}

    # ---------- WebSocket ----------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if token:
            got = websocket.cookies.get(COOKIE_NAME, "")
            header = websocket.headers.get("x-token")
            ok = got and secrets.compare_digest(
                got.encode("utf-8"), _session_value(token).encode("utf-8")
            )
            if not ok and not (
                header and secrets.compare_digest(header.encode("utf-8"), token.encode("utf-8"))
            ):
                await websocket.close(code=1008)
                return
        if not _same_origin(websocket.headers.get("origin"), websocket.headers.get("host")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(presence.status())
                await asyncio.sleep(2)
        except Exception:  # noqa: BLE001
            pass

    return app


async def start_web(cfg, storage, memory, presence) -> None:
    """以独立任务启动 uvicorn。"""
    import uvicorn

    app = create_app(cfg, storage, memory, presence)
    host = (cfg.web_cfg or {}).get("host", "127.0.0.1")
    port = int((cfg.web_cfg or {}).get("port", 11028))
    if not (cfg.web_cfg or {}).get("token"):
        logger.warning("web.token 为空：面板未启用鉴权，请仅在本机访问")
    server = uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port, log_level="warning", access_log=False, server_header=False
        )
    )
    logger.info("Web就绪: http://%s:%s", host, port)
    await server.serve()
