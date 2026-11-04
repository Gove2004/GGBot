"""Web 控制面板：FastAPI + WebSocket + 静态前端（ggbot/web/static）。

前端与后端分离：ggbot/web/static/{index.html,css/*.css,js/*.js}，经 /static 托管，可独立修改热加载。
功能：在线状态、会话与上下文、记忆（检索/写入/编辑/整理）、表情包浏览、提醒、配置热更新、日志查看。

鉴权（安全）：
- 登录：POST /api/login 校验 config.web.token，成功后写入 HttpOnly + SameSite=Strict Cookie，
  浏览器不再把令牌放进 URL 或 localStorage；
- API：Cookie 或 X-Token 头（CLI/脚本），常量时间比较；/ws 同样校验 Cookie；
- 限流：每个 IP 的 API 请求滑动窗口（240 次 / 10s），登录失败 8 次 / 60s 后返回 429；
  反代部署时设 web.trust_proxy=true，按 X-Forwarded-For 首个地址限流；
- 跨站 Origin 拦截、CSP 等安全响应头；令牌为空时仅建议本机使用。
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
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ggbot import __version__
from ggbot.core.auth import LEVEL_LOCKED, LEVEL_NAMES, LEVEL_MAX, clamp_level
from ggbot.core.config import BASE_DIR
from ggbot.core.runtime_stats import runtime
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
API_RATE_LIMIT = 240
API_RATE_WINDOW_SEC = 10
RATE_MAP_MAX = 2048
# 页面读数的两份「不跟筛选走」的固定量：会话清单一次给全、仪表盘最近消息 8 条
SESSIONS_LIMIT = 500
RECENT_LIMIT = 8

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


class MemoryIn(BaseModel):
    content: str
    kind: str = "profile"
    subject: str | None = None
    importance: int = 1
    session_id: str | None = None
    expires_at: str | None = None


class MemoryEditIn(BaseModel):
    content: str | None = None
    importance: int | None = None
    kind: str | None = None
    subject: str | None = None
    expires_at: str | None = None


class SettingIn(BaseModel):
    path: str
    value: object


class SystemPromptIn(BaseModel):
    prompt: str = ""


class EmojiDescIn(BaseModel):
    desc: str = ""


class ToolToggleIn(BaseModel):
    enabled: bool


class ActorIn(BaseModel):
    openid: str
    level: int = 2
    name: str | None = None
    note: str | None = None


class ToolLevelIn(BaseModel):
    level: int


class ScheduleEditIn(BaseModel):
    due_ts: int | None = None
    text: str | None = None


def _day_start_ts(now_ts: int) -> int:
    """服务器本地「今天零点」的 unix 秒（面板算「今天要发几条」按这个切）。"""
    lt = time.localtime(now_ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


# 允许通过 Web 热更新的配置路径（其余只读，避免误改关键结构）
ALLOWED_SETTINGS = [
    "llm.request.temperature",
    "llm.request.top_p",
    "llm.request.max_tokens",
    "llm.request.thinking",
    "behavior.presence.start_online",
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
    "behavior.backlog.max_scan_messages",
    "behavior.backlog.online_max_messages",
    "llm.context.max_history_messages",
    "llm.context.token_budget",
    "llm.context.archive_overflow_messages",
    "llm.context.archive_batch_messages",
    "llm.context.archive_enabled",
    "vision.enabled",
    "vision.model",
    "vision.thinking",
    "vision.prompt",
    "vision.max_chars",
    "vision.max_tokens",
    "vision.timeout_sec",
    "llm.pricing.currency",
    "llm.pricing.input_per_mtok",
    "llm.pricing.cached_input_per_mtok",
    "llm.pricing.output_per_mtok",
    "llm.pricing.peak_weekdays_only",
    "llm.pricing.off_peak_multiplier",
    "web.log_level",
]

# 白名单里仍需重启才生效的项：建连参数在进程启动时固定（面板据此打「需重启」标记）
RESTART_SETTINGS = [
    "vision.timeout_sec",
]

SECRET_KEYS = ("api_key", "secret", "appsecret", "token", "auth_code", "password")

# 系统提示词（人设）的字数上限：内置默认约 2.6k 字，留出重写和加设定的余量
SYSTEM_PROMPT_MAX_CHARS = 20000


def mask_secrets(node):
    """递归脱敏：密钥字段显示为 ***。"""
    if isinstance(node, dict):
        return {k: ("***" if k in SECRET_KEYS else mask_secrets(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [mask_secrets(v) for v in node]
    return node


def _env_info(cfg) -> dict:
    """面板「运行环境」摘要：模型名、服务域名与相对路径，不含任何密钥。"""
    def host_of(url) -> str:
        return urlparse(str(url or "")).netloc or ""

    def rel(path) -> str:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(BASE_DIR).as_posix()
        except ValueError:
            return resolved.as_posix()

    vision = cfg.vision
    embedding = cfg.embedding
    return {
        "chat_model": cfg.model or "-",
        "chat_host": host_of(cfg.llm.get("base_url")) or "-",
        "vision_model": vision["model"] if vision["enabled"] else "",
        "vision_host": host_of(vision["base_url"]),
        "memory_model": embedding["model"] if embedding["enabled"] else "",
        "memory_mode": "向量" if embedding["enabled"] else "关键词",
        "log_level": str(cfg.web_cfg.get("log_level", "DEBUG")),
        "db": rel(cfg.db_path),
        "temp": rel(cfg.temp_dir),
    }


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
    trust_proxy = bool((cfg.web_cfg or {}).get("trust_proxy", False))
    login_fails: dict[str, list[float]] = {}
    api_hits: dict[str, list[float]] = {}

    def _client_ip(request: Request) -> str:
        # 仅在明确启用反代时信任 X-Forwarded-For 的首个地址，避免直连时被伪造绕过限流
        if trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
        return request.client.host if request.client else "unknown"

    def _rate_limited(ip: str) -> int:
        """记录一次 API 请求；超过窗口上限时返回需等待的秒数。"""
        now = time.monotonic()
        hits = [t for t in api_hits.get(ip, []) if now - t < API_RATE_WINDOW_SEC]
        if len(hits) >= API_RATE_LIMIT:
            api_hits[ip] = hits
            return max(1, int(API_RATE_WINDOW_SEC - (now - hits[0])) + 1)
        hits.append(now)
        api_hits[ip] = hits
        if len(api_hits) > RATE_MAP_MAX:
            cutoff = now - API_RATE_WINDOW_SEC
            stale = [k for k, v in api_hits.items() if not v or v[-1] < cutoff]
            for key in stale:
                api_hits.pop(key, None)
            if len(api_hits) > RATE_MAP_MAX:
                overflow = len(api_hits) - RATE_MAP_MAX
                for key in sorted(api_hits, key=lambda k: api_hits[k][-1])[:overflow]:
                    api_hits.pop(key, None)
        return 0

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
            retry = max(1, int(LOGIN_WINDOW_SEC - (now - recent[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"尝试过于频繁，请 {retry} 秒后再试",
                headers={"Retry-After": str(retry)},
            )

    def _login_failed(ip: str) -> None:
        login_fails.setdefault(ip, []).append(time.monotonic())

    # 安全响应头 + IP 限流 + 跨站写请求拦截
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            retry = _rate_limited(_client_ip(request))
            if retry:
                resp = JSONResponse(
                    {"detail": f"请求过于频繁，请 {retry} 秒后再试"},
                    status_code=429,
                    headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
                )
                for k, v in SECURITY_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not _same_origin(request.headers.get("origin"), request.headers.get("host")):
                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # 前端文件是「改了刷新一下就生效」的设计：no-cache 带 ETag 回源校验，
            # 没改动照样 304，改动了不会拿着旧 JS 跑新页面
            response.headers["Cache-Control"] = "no-cache"
        return response

    # 静态前端：/static/css/*.css 与 /static/js/*.js（index.html 按顺序引用，加载顺序 = 原来单文件的行序）
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # 版本号只有一个来源（ggbot.__version__）：前端 index.html 保持可独立热改，
        # 页面上的 {{VERSION}} 占位在返回时替换，不留一份会过期的硬编码
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{VERSION}}", __version__))

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
        ip = _client_ip(request)
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

    # ---------- 外壳读数 ----------

    @app.get("/api/shell")
    async def shell(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        # 在线态只算一次：以前 /api/status 与 /api/stats 各自跑一遍 presence.status()，
        # 面板每秒打两次卡，两份读数还可能对不上
        live = presence.status()
        out = storage.stats()
        out["online_sessions"] = live.get("online_sessions", 0)
        out["state"] = live.get("state", "offline")
        out["runtime"] = runtime.snapshot()
        out["env"] = _env_info(cfg)
        return {"status": live, "stats": out}

    # ---------- 会话详情 ----------

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
            "messages": storage.get_recent_messages(sid, min(max(limit, 1), 500)),
            # 出站回执：被动回复有没有被平台拒、什么时候降成了主动，看这一份就够，不用翻日志
            "sends": storage.recent_sends(sid, 10),
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

    # ---------- 表情包 ----------
    # 素材目录是唯一真相，工具与面板读写同一份索引/向量/使用统计；这里只是开了第二条入口，
    # 写入全部走 EmojiLibrary（去重、上限、向量重算都在它身上，面板不另存一套状态）。
    # 一个统一库：不分「内置/收藏」，图在哪个子目录只体现在 file 里。

    def _emoji_lib():
        ex = getattr(presence, "executor", None)
        tool = ex.registry.tools.get("send_emoji") if ex is not None else None
        return getattr(tool, "lib", None)

    def _emoji_lib_or_503():
        lib = _emoji_lib()
        if lib is None:
            raise HTTPException(503, "表情工具未启用（工具页里打开 send_emoji 才能管理素材）")
        return lib

    @app.post("/api/emojis/import")
    async def emoji_import(
        request: Request,
        name: str = Query(default=""),
        desc: str = Query(default=""),
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        lib = _emoji_lib_or_503()
        data = await request.body()
        out = await lib.import_image(name, data, desc)
        if not out.get("ok"):
            raise HTTPException(400, out.get("error") or "导入失败")
        return out

    @app.put("/emoji/{rel:path}")
    async def emoji_desc(
        rel: str,
        body: EmojiDescIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        lib = _emoji_lib_or_503()
        out = await lib.set_desc(rel, body.desc or "")
        if out is None:
            raise HTTPException(404, "素材不在库里")
        if not out.get("ok"):
            raise HTTPException(400, out.get("error") or "描述没存下")
        return out

    @app.delete("/emoji/{rel:path}")
    async def emoji_delete(
        rel: str, request: Request, x_token: str | None = Header(default=None)
    ):
        _auth(request, x_token)
        lib = _emoji_lib_or_503()
        if not await lib.remove(rel):
            raise HTTPException(404, "素材不在库里")
        return {"ok": True, "removed": rel}

    # 缩略图不放 /api 下：一屏几十张图会被 API 的 IP 限流（240 次/10s）误伤
    @app.get("/emoji/{rel:path}", response_class=FileResponse)
    async def emoji_image(
        rel: str, request: Request, x_token: str | None = Header(default=None)
    ):
        _auth(request, x_token)
        lib = _emoji_lib()
        p = lib.image_path(rel) if lib is not None else None
        if p is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            p,
            headers={"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"},
        )

    # ---------- 日程（提醒） ----------
    # 表里的那份是唯一真相（巡检循环也读它），所以面板给「看 + 改 + 删 + 清理」，
    # 刻意不给新建：面板排日程要的是另一套时间输入，而 when/repeat 的解析只该有一份实现。

    def _schedule_or_none():
        """日程的操作面；工具没挂载就返回 None（配置读的是 tool/schedule/config.json）。"""
        rt = getattr(getattr(presence, "executor", None), "rt", None)
        if rt is None:
            return None
        from tool.schedule.remind import Reminders

        return Reminders(rt)

    def _schedule():
        rem = _schedule_or_none()
        if rem is None:
            raise HTTPException(503, "日程工具未挂载，暂时改不了日程")
        return rem

    @app.post("/api/reminders/{rid}/cancel")
    async def cancel_reminder(
        rid: int,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        row = await asyncio.to_thread(storage.get_reminder, rid)
        if not row:
            raise HTTPException(status_code=404, detail="reminder not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"#{rid} 已经是 {row['status']}，不用再取消")
        # finish_reminder 只动 pending 的行，置为 cancelled 后巡检就不会再触发它
        await asyncio.to_thread(storage.finish_reminder, rid, "cancelled")
        return {"ok": True}

    @app.put("/api/reminders/{rid}")
    async def edit_reminder(
        rid: int,
        body: ScheduleEditIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        """改时间/正文：只有还在排的行改得了，改完仍是 pending（周期项改的是下一场）。"""
        _auth(request, x_token)
        text = None if body.text is None else " ".join(str(body.text).split())
        if body.due_ts is None and text is None:
            raise HTTPException(status_code=400, detail="没有需要修改的字段")
        cfg = _schedule().config
        now = int(time.time())
        if text is not None:
            cap = max(10, int(cfg.get("max_text_chars", 200) or 200))
            if not text:
                raise HTTPException(status_code=400, detail="正文不能为空")
            if len(text) > cap:
                raise HTTPException(status_code=400, detail=f"正文太长（最多 {cap} 字）")
        if body.due_ts is not None:
            due = int(body.due_ts)
            if due <= now:
                raise HTTPException(status_code=400, detail="新时间得是将来——改到过去等于现在就发")
            max_days = max(1, int(cfg.get("max_days", 30) or 30))
            if due > now + max_days * 86400:
                raise HTTPException(status_code=400, detail=f"最远只能约 {max_days} 天内的第一次触发")
        row = await asyncio.to_thread(storage.get_reminder, rid)
        if not row:
            raise HTTPException(status_code=404, detail="reminder not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"#{rid} 已经结束了（{row['status']}），改不了")
        changed = await asyncio.to_thread(storage.update_reminder, rid, body.due_ts, text)
        if not changed:
            raise HTTPException(status_code=409, detail=f"#{rid} 刚好被巡检触发了，刷新看看")
        return {"ok": True}

    @app.delete("/api/reminders/{rid}")
    async def delete_reminder(
        rid: int,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        """物理删一行：取消才是"不再触发"，删除只是把这条记录从表里拿掉。"""
        _auth(request, x_token)
        if not await asyncio.to_thread(storage.delete_reminder, rid):
            raise HTTPException(status_code=404, detail="reminder not found")
        return {"ok": True}

    @app.delete("/api/reminders")
    async def purge_reminders(
        request: Request,
        status: str | None = None,
        session_id: str | None = None,
        before_days: int | None = None,
        x_token: str | None = Header(default=None),
    ):
        """批量清历史：只碰终态行。条件全空时什么都不删（待触发的要退场得走取消）。"""
        _auth(request, x_token)
        if status == "pending":
            raise HTTPException(status_code=400, detail="待触发的行不能批量删，请先取消")
        if not any((status, session_id, before_days)):
            raise HTTPException(status_code=400, detail="需要 status / session_id / before_days 至少一个条件")
        before_ts = None
        if before_days:
            before_ts = int(time.time()) - max(0, int(before_days)) * 86400
        removed = await asyncio.to_thread(
            storage.purge_reminders, session_id or None, status or None, before_ts)
        return {"ok": True, "removed": removed}

    @app.post("/api/reminders/sweep")
    async def sweep_reminders(request: Request, x_token: str | None = Header(default=None)):
        """按保留天数与总行数上限清历史（pending 不动），和巡检循环里的定时清理同一条路。"""
        _auth(request, x_token)
        return await asyncio.to_thread(_schedule().sweep)

    # ---------- 记忆 ----------

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
        out = await asyncio.to_thread(memory.commit, [{
            "content": content,
            "kind": body.kind if body.kind in ("profile", "event") else "profile",
            "subject": body.subject or "",
            "importance": max(1, min(5, body.importance)),
            "session_id": body.session_id or None,
            "expires_at": body.expires_at or None,
        }])
        return {"ok": True, "written": out["written"], "updated": out["updated"],
                "dropped": out["dropped"], "retired": out["retired"]}

    @app.put("/api/memories/{memory_id}")
    async def memory_edit(
        memory_id: str,
        body: MemoryEditIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        if not any(v is not None for v in (body.content, body.importance,
                                           body.kind, body.subject, body.expires_at)):
            raise HTTPException(status_code=400, detail="没有需要修改的字段")
        if body.kind is not None and body.kind not in ("profile", "event"):
            raise HTTPException(status_code=400, detail="kind 只能是 profile 或 event")
        try:
            ok = await asyncio.to_thread(
                memory.update,
                memory_id,
                body.content,
                None if body.importance is None else max(1, min(5, body.importance)),
                body.subject,
                body.kind,
                body.expires_at,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not ok:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True}

    @app.post("/api/memories/sweep")
    async def memory_sweep(request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        return await asyncio.to_thread(memory.sweep)

    @app.delete("/api/memories")
    async def memory_delete_group(
        request: Request,
        subject: str | None = None,
        kind: str | None = None,
        session_id: str | None = None,
        x_token: str | None = Header(default=None),
    ):
        """整组清理：按「谁 / 哪类 / 哪个会话」删。三个条件全空时什么都不删（清空整库走 reset）。"""
        _auth(request, x_token)
        if not any((subject, kind, session_id)):
            raise HTTPException(status_code=400, detail="需要 subject / kind / session_id 至少一个条件")
        if kind is not None and kind not in ("profile", "event"):
            raise HTTPException(status_code=400, detail="kind 只能是 profile 或 event")
        removed = storage.delete_memories_by(session_id or None, subject or None, kind or None)
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

    # ---------- 工具（插件清单 / 启停） ----------

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

    # ---------- 权限（成员等级 / 工具门槛） ----------

    def _perms():
        p = getattr(presence, "perms", None)
        if p is None:
            raise HTTPException(status_code=503, detail="权限模块未就绪")
        return p

    @app.post("/api/actors")
    async def set_actor(
        body: ActorIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        openid = body.openid.strip()
        if not openid:
            raise HTTPException(status_code=400, detail="openid 不能为空")
        if not 0 <= int(body.level) <= LEVEL_MAX:
            raise HTTPException(status_code=400, detail=f"等级需在 0-{LEVEL_MAX} 之间")
        p = _perms()
        row = p.set_level(openid, body.level, note=body.note, name=body.name)
        logger.info("Web 权限设置 %s = L%d %s", openid[:8], row.get("level"),
                    LEVEL_NAMES.get(clamp_level(row.get("level")), ""))
        return {"ok": True, "actor": row}

    @app.post("/api/tools/{name}/level")
    async def set_tool_level(
        name: str,
        body: ToolLevelIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        if not 1 <= int(body.level) <= LEVEL_LOCKED:
            raise HTTPException(status_code=400, detail="工具门槛需在 1-6 之间（6 = 永久锁）")
        ex = getattr(presence, "executor", None)
        tool = ex.registry.tools.get(name) if ex is not None else None
        if tool is None:
            raise HTTPException(status_code=404, detail="tool not found")
        data = ex.set_level(name, body.level)
        level = tool.required_level()
        logger.info("Web 工具门槛 %s = L%d%s", name, level, "（永久锁）" if level >= LEVEL_LOCKED else "")
        return {"ok": True, "level": level, "config": mask_secrets(data)}

    # ---------- 配置（Web 热更新） ----------

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
            else:
                rejected.append(item.path)
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

    # ---------- 系统提示词（人设） ----------

    def _system_prompt_payload() -> dict:
        from ggbot.core.presence import SYSTEM_TEMPLATE

        custom = str(cfg.llm.get("system_prompt") or "")
        return {
            "custom": custom,
            "default": SYSTEM_TEMPLATE,
            # 面板编辑框里显示的就是这一份：自定义优先，否则内置默认
            "effective": custom.strip() or SYSTEM_TEMPLATE,
            "is_custom": bool(custom.strip()),
            "max_chars": SYSTEM_PROMPT_MAX_CHARS,
        }

    @app.post("/api/system-prompt")
    async def update_system_prompt(
        body: SystemPromptIn,
        request: Request,
        x_token: str | None = Header(default=None),
    ):
        _auth(request, x_token)
        text = body.prompt or ""
        if len(text) > SYSTEM_PROMPT_MAX_CHARS:
            raise HTTPException(
                400, f"系统提示词最长 {SYSTEM_PROMPT_MAX_CHARS} 字，当前 {len(text)} 字"
            )
        if not cfg.set_by_path("llm.system_prompt", text):
            raise HTTPException(500, "配置里没有 llm 段，存不下系统提示词")
        cfg.save()
        logger.info("Web 系统提示词: %s（%d 字）",
                    "自定义" if text.strip() else "回到内置默认", len(text))
        return _system_prompt_payload()

    # ---------- 页面读数：一进页面一次拿齐 ----------
    # 面板的读全部收在 GET /api/page/<name>：一次往返就是这一页要画的完整快照，
    # 筛选/分页参数原样带在 query 上，口径校验留在各页自己的 loader 里。
    # 写操作不跟着动——仍然按资源走，改完由前端重新拉一次这一页。
    # 一份数据一个名字（sessions / memories / reminders…），多页共用的那份只有一份实现。

    class _Q:
        """query 参数的取值口径：缺省、非法、越界都在这层收掉，loader 里只留业务。"""

        def __init__(self, request: Request):
            self._d = request.query_params

        def text(self, key: str) -> str:
            return (self._d.get(key) or "").strip()

        def num(self, key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
            raw = self._d.get(key)
            try:
                n = default if raw in (None, "") else int(raw)
            except ValueError:
                n = default
            if lo is not None:
                n = max(lo, n)
            if hi is not None:
                n = min(hi, n)
            return n

    # 会话清单与最近消息不带参数：面板要的永远是「全部会话 + 最近 8 条」，
    # 参数留着只会让「记忆页的 limit」和「会话清单的 limit」撞名。
    async def _ds_sessions(q: _Q) -> dict:
        return {"sessions": await asyncio.to_thread(_live_sessions, SESSIONS_LIMIT)}

    def _live_sessions(limit: int) -> list[dict]:
        rows = storage.list_sessions(limit)
        live = {s["sid"]: s for s in (presence.status().get("sessions") or [])}
        for row in rows:
            row["label"] = session_label(row.get("id"))
            st = live.get(row.get("id"))
            row["live"] = st or {"state": "offline", "reason": "idle",
                                 "remaining_sec": 0, "idle_sec": None}
        return rows

    async def _ds_recent(q: _Q) -> dict:
        return {"recent": await asyncio.to_thread(storage.recent_messages_global, RECENT_LIMIT)}

    async def _ds_logs(q: _Q) -> dict:
        from ggbot.util.logging_setup import tail_log

        level = q.text("level") or "DEBUG"
        session, needle = q.text("session"), q.text("q")
        n = q.num("lines", 200, 1, 2000)
        # 会话/内容筛选要先筛后截：先取最近 n 行再筛，等于只能在这么宽的窗口里搜，
        # 窗口外的日志永远筛不出来（等级门槛在 tail_log 里已经先筛过）
        raw = tail_log(cfg.log_file, None if (session or needle) else n, level)
        ses, want = session.lower(), needle.lower()
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
                        ts, lv, sess, content = "", "-", SYSTEM_LABEL, line
                    else:
                        ts, lv, _mod, content = m.groups()
                        sess = SYSTEM_LABEL
            if ses and ses not in sess.lower():
                continue
            if want and want not in content.lower():
                continue
            logs.append({"ts": ts[:23], "level": lv, "session": sess, "content": content})
        total = len(logs)
        try:
            log_name = cfg.log_file.relative_to(cfg.log_file.parent.parent.parent).as_posix()
        except ValueError:
            log_name = cfg.log_file.name
        return {"logs": {"level": level, "file": log_name, "logs": logs[-n:], "total": total}}

    async def _ds_reminders(q: _Q) -> dict:
        from tool.schedule._rule import rule_label as _rule_label

        st = q.text("status") or "pending"
        st = None if st == "all" else st
        sid, needle = q.text("session_id") or None, q.text("q") or None
        offset = q.num("offset", 0, 0)
        rows = await asyncio.to_thread(
            storage.list_reminders, sid, st, q.num("limit", 200, 1, 500), offset, needle)
        total = storage.count_reminders(sid, st, needle)
        now = int(time.time())
        stats = await asyncio.to_thread(storage.reminder_stats, now, _day_start_ts(now))
        for r in rows:
            r["rule_label"] = _rule_label(r.get("rule") or "")
        rem = _schedule_or_none()
        tool_cfg = rem.config if rem else {}
        return {"reminders": {
            "items": rows, "total": total, "offset": offset, "stats": stats,
            "limits": {k: tool_cfg.get(k) for k in
                       ("max_pending", "max_days", "max_text_chars",
                        "keep_history_days", "max_rows", "miss_grace_sec")}}}

    async def _ds_memories(q: _Q) -> dict:
        sid, subject, kind = q.text("session_id") or None, q.text("subject") or None, q.text("kind") or None
        needle = q.text("q")
        mcfg = memory.m
        out = {
            "limits": {"max_memories": mcfg["max_memories"],
                       "max_per_subject": mcfg["max_per_subject"],
                       "forget_after_days": mcfg["forget_after_days"]},
            # 整库容量读数与筛选无关，而且已经按遗忘扫描的口径扣掉到期/淡出
            "usage": await asyncio.to_thread(memory.usage),
            "searched": bool(needle),
        }
        if needle:
            # 「检索」也只列命中，但 limits/usage 照样带上——临界提示在检索时说的是同一套口径
            hits = await asyncio.to_thread(
                memory.search, needle, q.num("k", 50, 1, 50), sid, None)
            out["items"] = await asyncio.to_thread(memory.panel_rows, hits)
            out["total"] = len(out["items"])
        else:
            rows = await asyncio.to_thread(
                storage.list_memories, sid, q.num("limit", 200, 1, 500), q.num("offset", 0, 0),
                subject, kind)
            out["items"] = await asyncio.to_thread(memory.panel_rows, rows)
            out["total"] = storage.count_memories(sid, subject, kind)
        return {"memories": out}

    async def _ds_emojis(q: _Q) -> dict:
        lib = _emoji_lib()
        if lib is None:
            return {"emojis": {"items": [], "mode": "none", "max_stickers": 0, "max_image_bytes": 0}}
        # force=1：面板「刷新」按钮要立刻看到刚丢进目录的图；平时用扫目录缓存
        rows = await lib.rows(force=bool(q.num("force", 0, 0, 1)))
        return {"emojis": {
            "mode": lib.matcher.mode,
            "max_stickers": int(lib.config.get("max_stickers", 0) or 0),
            "max_image_bytes": lib.max_image_bytes,
            "items": [
                {
                    "file": r["file"],
                    "desc": r["desc"],
                    "added_at": r["added_at"],
                    "use_count": r["use_count"],
                    "last_used_at": r["last_used_at"],
                    "has_vec": r["vec"] is not None,
                    "url": f"/emoji/{quote(r['file'])}",
                }
                for r in rows
            ],
        }}

    async def _ds_tools(q: _Q) -> dict:
        ex = getattr(presence, "executor", None)
        if ex is None:
            return {"tools": []}
        rows = ex.list_tools()
        for row in rows:
            # 密钥只在这里脱敏：工具配置既给「工具」页也给「权限」页，两份都得是脱敏后的
            row["config"] = mask_secrets(row.get("config") or {})
        return {"tools": rows}

    async def _ds_system_prompt(q: _Q) -> dict:
        return {"system_prompt": _system_prompt_payload()}

    async def _ds_settings(q: _Q) -> dict:
        return {"settings": {"config": mask_secrets(cfg.data), "allowed": ALLOWED_SETTINGS,
                             "restart": RESTART_SETTINGS}}

    async def _ds_permissions(q: _Q) -> dict:
        p = _perms()
        return {"permissions": {
            "members": p.members(),
            "default_level": p.default_level,
            "level_names": {str(k): v for k, v in LEVEL_NAMES.items()},
            "max_level": LEVEL_MAX,
            "locked_level": LEVEL_LOCKED,
        }}

    # 一页要读哪几份数据，全在这张表里；前端 PAGE_META 有同名条目才拉得到数据
    PAGES: dict[str, list] = {
        "dash": [_ds_sessions, _ds_recent],
        "logs": [_ds_logs],
        "sessions": [_ds_sessions],
        "schedule": [_ds_sessions, _ds_reminders],
        "memory": [_ds_sessions, _ds_memories],
        "emojis": [_ds_emojis],
        "persona": [_ds_system_prompt],
        "settings": [_ds_settings],
        "permissions": [_ds_permissions, _ds_tools],
        "tools": [_ds_tools],
    }

    @app.get("/api/page/{name}")
    async def page_data(name: str, request: Request, x_token: str | None = Header(default=None)):
        _auth(request, x_token)
        loaders = PAGES.get(name)
        if loaders is None:
            raise HTTPException(status_code=404, detail="没有这一页")
        q = _Q(request)
        out: dict = {}
        for load in loaders:
            out.update(await load(q))
        return out

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
    logger.info("Web 就绪: http://%s:%s", host, port)
    await server.serve()
