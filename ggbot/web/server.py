"""Web 控制面板：FastAPI + 静态前端（ggbot/web/static）。

后端按「谁负责哪一面」分成五块，create_app 只做装配：
- server.py    装配：安全中间件、静态托管、首页与签名媒体，再把下面四张 router 挂上去
- guards.py    安全层：令牌/Cookie 鉴权、按 IP 限流、跨站拦截、密钥脱敏、登录三条路由；
               Panel 还是面板伸进运行中对象的唯一一只把手（工具注册表/表情库/任务/权限）
- readers.py   读数：GET /api/page/<name>，一进页面一次拿齐的快照
- resources.py 资源：会话、表情素材、任务、记忆——动的是对话产生的内容
- settings.py  控制：工具启停与门槛、成员等级、配置热更新、人设——动的是行为

鉴权不写在处理函数里：受保护的 router 统一挂依赖，新增一条路由自动带上门禁，
不存在「忘了调鉴权」这种漏法；登录、首页、签名媒体这些公开面留在门禁之外。

前端与后端分离：ggbot/web/static/{index.html,css/*.css,js/*.js}，经 /static 托管，可独立修改热加载。
功能：在线状态、会话与上下文、记忆（检索/写入/编辑/整理）、表情包浏览、定时任务、配置热更新、日志查看。

鉴权（安全）：
- 登录：POST /api/login 校验 config.web.token，成功后写入 HttpOnly + SameSite=Strict Cookie，
  浏览器不再把令牌放进 URL 或 localStorage；
- API：Cookie 或 X-Token 头（CLI/脚本），常量时间比较；
- 限流：每个 IP 的 API 请求滑动窗口（240 次 / 10s），登录失败 8 次 / 60s 后返回 429；
  反代部署时设 web.trust_proxy=true，按 X-Forwarded-For 首个地址限流；
- 跨站 Origin 拦截、CSP 等安全响应头；令牌为空时仅建议本机使用。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ggbot import __version__
from ggbot.web import guards, readers, resources, settings
from ggbot.web.guards import SECURITY_HEADERS, Panel

logger = logging.getLogger("ggbot.web")

WEB_DIR = Path(__file__).resolve().parent / "static"


def create_app(cfg, storage, memory, presence) -> FastAPI:
    app = FastAPI(title="GGBot Panel", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.panel = Panel(cfg, storage, memory, presence)

    # 安全响应头 + IP 限流 + 跨站写请求拦截
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        p = request.app.state.panel
        if request.url.path.startswith("/api/"):
            retry = p.rate_limited(p.client_ip(request))
            if retry:
                resp = JSONResponse(
                    {"detail": f"请求过于频繁，请 {retry} 秒后再试"},
                    status_code=429,
                    headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
                )
                for k, v in SECURITY_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp
        if request.method not in ("GET", "HEAD", "OPTIONS") and not guards.same_origin(request):
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

    # 本地媒体（QQ 富媒体上传用的签名 URL，无需 Cookie）
    @app.get("/media/{exp}/{sig}/{rel:path}", response_class=FileResponse)
    async def media(exp: int, sig: str, rel: str):
        from ggbot.util.media import verify_path

        path = verify_path(cfg, exp, sig, rel)
        if not path:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            path,
            headers={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"},
        )

    # 公开面（登录那三条）在门禁之外，其余三张 router 自带鉴权依赖
    app.include_router(guards.open_router)
    for module in (resources, settings, readers):
        app.include_router(module.router)
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
