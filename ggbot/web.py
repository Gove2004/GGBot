"""Web 控制面板：FastAPI + WebSocket + 静态前端（项目根 web/ 目录）。

前端与后端分离：web/index.html + web/style.css + web/app.js，经 /static 托管，可独立修改热加载。
功能：在线状态、消息流、记忆（向量检索/写入/整理）、规则管理、人设查看与回滚、配置热更新、日志查看。
鉴权：config.web.token，所有 /api 请求需带 ?token= 或 X-Token 头。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("ggbot.web")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class RuleIn(BaseModel):
    content: str
    trigger: str | None = None


class MemoryIn(BaseModel):
    content: str
    importance: int = 1


class RollbackIn(BaseModel):
    layer: str
    version: int


class SettingIn(BaseModel):
    path: str
    value: object


# 允许通过 Web 热更新的配置路径（其余只读，避免误改关键结构）
ALLOWED_SETTINGS = [
    "deepseek.request.temperature",
    "deepseek.request.top_p",
    "deepseek.request.max_tokens",
    "deepseek.request.thinking",
    "deepseek.request.stream",
    "behavior.presence.online_duration_sec",
    "behavior.presence.offline_interval_sec",
    "behavior.presence.wake_on_at_message",
    "behavior.presence.wake_on_c2c",
    "behavior.presence.renew_on_reply",
    "behavior.reply.max_segments",
    "behavior.reply.max_chars_per_segment",
    "behavior.reply.segment_gap_sec",
    "behavior.reply.debounce_sec",
    "behavior.backlog.max_scan_messages",
    "behavior.backlog.batch_size",
    "behavior.backlog.priority_rule",
    "web.log_level",
]

SECRET_KEYS = ("api_key", "secret", "appsecret", "token")


def mask_secrets(node):
    """递归脱敏：密钥字段显示为 ***。"""
    if isinstance(node, dict):
        return {k: ("***" if k in SECRET_KEYS else mask_secrets(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [mask_secrets(v) for v in node]
    return node


def create_app(cfg, storage, memory, presence) -> FastAPI:
    app = FastAPI(title="GGBot Panel")
    token = (cfg.web_cfg or {}).get("token", "")

    def _auth(x_token: str | None, t: str | None) -> None:
        if token and x_token != token and t != token:
            raise HTTPException(status_code=401, detail="invalid token")

    # 静态前端：web/index.html + /static/style.css + /static/app.js
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/status")
    async def status(x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return presence.status()

    @app.get("/api/messages")
    async def messages(limit: int = 30, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return storage.recent_messages_global(limit)

    @app.get("/api/memories")
    async def memories(limit: int = 20, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return storage.list_memories(limit=limit)

    @app.get("/api/memories/search")
    async def memory_search(q: str, k: int = 5, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return await asyncio.to_thread(memory.search, q, k)

    @app.post("/api/memories")
    async def memory_add(body: MemoryIn, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        mid = await asyncio.to_thread(memory.add, body.content, "fact", body.importance)
        return {"ok": True, "memory_id": mid}

    @app.post("/api/memories/tidy")
    async def memory_tidy(x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        removed = await asyncio.to_thread(memory.tidy)
        return {"ok": True, "removed": removed}

    @app.delete("/api/memories/{memory_id}")
    async def memory_delete(memory_id: str, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        storage.delete_memory(memory_id)
        return {"ok": True}

    @app.get("/api/rules")
    async def rules(x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return storage.list_rules(active_only=False)

    @app.post("/api/rules")
    async def rule_add(body: RuleIn, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        rid = storage.add_rule(body.content, body.trigger)
        return {"ok": True, "rule_id": rid}

    @app.post("/api/rules/{rule_id}/toggle")
    async def rule_toggle(rule_id: int, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        row = next((r for r in storage.list_rules(active_only=False) if r["id"] == rule_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        storage.set_rule_active(rule_id, not row["active"])
        return {"ok": True}

    @app.get("/api/persona")
    async def persona(x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        def _latest(layer: str) -> dict:
            versions = storage.list_persona_versions(layer, 1)
            return {"content": storage.get_persona(layer), "version": versions[0]["version"] if versions else 0,
                    "history": storage.list_persona_versions(layer, 10)}
        return {"inner": _latest("inner"), "dynamic": _latest("dynamic")}

    @app.post("/api/persona/rollback")
    async def persona_rollback(body: RollbackIn, x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        content = storage.rollback_persona(body.layer, body.version)
        if content is None:
            raise HTTPException(status_code=404, detail="version not found")
        return {"ok": True, "content": content}

    # ---------- 配置（Web 热更新） ----------

    @app.get("/api/config")
    async def get_config(x_token: str | None = Header(default=None), token: str | None = Query(default=None)):
        _auth(x_token, token)
        return {"config": mask_secrets(cfg.data), "allowed": ALLOWED_SETTINGS}

    @app.post("/api/config")
    async def update_config(
        body: list[SettingIn], x_token: str | None = Header(default=None), token: str | None = Query(default=None)
    ):
        _auth(x_token, token)
        updated: list[str] = []
        for item in body:
            if item.path not in ALLOWED_SETTINGS:
                continue
            if cfg.set_by_path(item.path, item.value):
                updated.append(item.path)
        if "web.log_level" in updated:
            from ggbot.logging_setup import set_level

            set_level(cfg.data["web"]["log_level"])
        cfg.save()
        logger.info("Web 配置更新: %s", updated)
        return {"ok": True, "updated": updated, "config": mask_secrets(cfg.data)}

    # ---------- 日志查看 ----------

    @app.get("/api/logs")
    async def get_logs(
        lines: int = 200, level: str = "INFO",
        x_token: str | None = Header(default=None), token: str | None = Query(default=None),
    ):
        _auth(x_token, token)
        from ggbot.logging_setup import tail_log

        n = min(max(lines, 1), 2000)
        return {"level": level, "lines": tail_log(cfg.log_file, n, level)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
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
    port = int((cfg.web_cfg or {}).get("port", 8080))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    logger.info("Web 面板启动: http://%s:%s（前端目录 %s）", host, port, WEB_DIR)
    await server.serve()
