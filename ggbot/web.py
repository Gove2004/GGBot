"""Web 控制面板：FastAPI + WebSocket。

功能：在线状态、消息流、记忆（向量检索/写入/整理）、规则管理、人设查看与回滚。
鉴权：config.web.token，所有 /api 请求需带 ?token= 或 X-Token 头。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger("ggbot.web")

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>GGBot 控制面板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#dbe2ec;--dim:#8a93a3;--acc:#4f8cff;--ok:#3ecf8e;--warn:#ffb454}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 "Segoe UI",system-ui,sans-serif;padding:24px}
h1{font-size:18px;margin-bottom:4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h2{font-size:13px;color:var(--dim);font-weight:600;margin-bottom:12px;letter-spacing:.5px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}
.on{background:rgba(62,207,142,.15);color:var(--ok)}
.off{background:rgba(255,180,84,.15);color:var(--warn)}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--line)}
.row:last-child{border-bottom:none}
.row b{font-weight:500}
.lbl{color:var(--dim)}
pre{background:#0d0f13;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;max-height:260px;font-size:12px;white-space:pre-wrap;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500}
input[type=text]{background:#0d0f13;border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:7px 10px;width:100%;margin-bottom:8px}
button{background:var(--acc);color:#fff;border:none;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:12px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--dim)}
.muted{color:var(--dim);font-size:12px}
.mt{margin-top:12px}
.tag{display:inline-block;background:rgba(79,140,255,.15);color:#9cc0ff;border-radius:4px;padding:0 6px;font-size:11px;margin-right:6px}
</style>
</head>
<body>
<h1>GGBot 控制面板</h1>
<div class="sub">QQ 群聊机器人 · 在线/离线作息 · 向量记忆 · 人设进化</div>

<div class="grid">
  <div class="card">
    <h2>运行状态</h2>
    <div class="row"><span class="lbl">状态</span><span id="st-state" class="badge off">加载中…</span></div>
    <div class="row"><span class="lbl">下一个事件</span><b id="st-event">—</b></div>
    <div class="row"><span class="lbl">剩余秒</span><b id="st-remain">—</b></div>
    <div class="row"><span class="lbl">积压待处理</span><b id="st-pending">—</b></div>
    <div class="row"><span class="lbl">抖动窗口内</span><b id="st-debounce">—</b></div>
    <div class="row"><span class="lbl">在线时长 / 离线周期</span><b id="st-dur">—</b></div>
    <div class="row"><span class="lbl">聊得火热(续期)</span><b id="st-renew">—</b></div>
    <div class="row"><span class="lbl">积压扫描/合并</span><b id="st-backlog">—</b></div>
  </div>

  <div class="card">
    <h2>最近消息</h2>
    <table><thead><tr><th>会话</th><th>角色</th><th>内容</th><th>状态</th></tr></thead>
    <tbody id="msg-body"></tbody></table>
  </div>

  <div class="card">
    <h2>向量记忆</h2>
    <input type="text" id="mem-q" placeholder="语义检索：输入要回忆的内容…">
    <button onclick="searchMem()">检索</button>
    <button class="ghost" onclick="tidyMem()">整理(合并重复)</button>
    <div class="mt">
      <table><thead><tr><th>相似度</th><th>内容</th><th></th></tr></thead>
      <tbody id="mem-body"></tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>规则</h2>
    <input type="text" id="rule-content" placeholder="新增规则，如：禁止在群里讨论政治">
    <button onclick="addRule()">添加</button>
    <table class="mt"><thead><tr><th>规则</th><th>状态</th><th></th></tr></thead>
    <tbody id="rule-body"></tbody></table>
  </div>

  <div class="card">
    <h2>人设（双层 + 版本化）</h2>
    <div class="row"><span class="lbl">内核人设（只读）</span><span id="p-inner-v">—</span></div>
    <pre id="p-inner">—</pre>
    <div class="row mt"><span class="lbl">动态人设（AI 可进化）</span><span id="p-dyn-v">—</span></div>
    <pre id="p-dyn">—</pre>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const Q = (u, o={}) => fetch(u + (TOKEN ? (u.includes("?") ? "&" : "?") + "token=" + TOKEN : ""), o).then(r => r.json());
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;" })[c]);

async function refreshStatus() {
  const s = await Q("/api/status");
  const el = document.getElementById("st-state");
  el.textContent = s.state === "online" ? "在线" : "离线";
  el.className = "badge " + (s.state === "online" ? "on" : "off");
  document.getElementById("st-event").textContent = s.next_event;
  document.getElementById("st-remain").textContent = s.remaining_sec;
  document.getElementById("st-pending").textContent = s.pending_count;
  document.getElementById("st-debounce").textContent = s.debounce_count;
  document.getElementById("st-dur").textContent = `${s.online_duration_sec}s / ${s.offline_interval_sec}s`;
  document.getElementById("st-renew").textContent = s.renew_on_reply ? "开启" : "关闭";
  document.getElementById("st-backlog").textContent = `${s.backlog_max_scan} / ${s.backlog_batch_size}`;
}
async function refreshMessages() {
  const d = await Q("/api/messages?limit=30");
  document.getElementById("msg-body").innerHTML = d.map(m =>
    `<tr><td class="muted">${esc(m.skey)}</td><td>${esc(m.role)}</td><td>${esc((m.content||"").slice(0,60))}</td><td class="muted">${esc(m.status)}</td></tr>`).join("");
}
async function refreshMemories(q) {
  const d = await Q(q ? "/api/memories/search?q=" + encodeURIComponent(q) + "&k=8" : "/api/memories?limit=10");
  document.getElementById("mem-body").innerHTML = d.map(m =>
    `<tr><td class="muted">${m.score != null ? m.score : "—"}</td><td>${esc(m.content)}</td>` +
    `<td><button class="ghost" onclick="delMem('${m.id}')">删</button></td></tr>`).join("");
}
async function searchMem() { refreshMemories(document.getElementById("mem-q").value); }
async function tidyMem() { const r = await Q("/api/memories/tidy", { method: "POST" }); alert("合并删除 " + r.removed + " 条"); refreshMemories(); }
async function delMem(id) { await Q("/api/memories/" + id, { method: "DELETE" }); refreshMemories(); }
async function refreshRules() {
  const d = await Q("/api/rules");
  document.getElementById("rule-body").innerHTML = d.map(r =>
    `<tr><td>${esc(r.content)}</td><td class="muted">${r.active ? "启用" : "停用"}</td>` +
    `<td><button class="ghost" onclick="toggleRule(${r.id})">切换</button></td></tr>`).join("");
}
async function addRule() {
  const v = document.getElementById("rule-content").value.trim();
  if (!v) return;
  await Q("/api/rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: v }) });
  document.getElementById("rule-content").value = "";
  refreshRules();
}
async function toggleRule(id) { await Q("/api/rules/" + id + "/toggle", { method: "POST" }); refreshRules(); }
async function refreshPersona() {
  const d = await Q("/api/persona");
  document.getElementById("p-inner-v").textContent = "v" + (d.inner.version ?? "?");
  document.getElementById("p-inner").textContent = d.inner.content || "（无）";
  document.getElementById("p-dyn-v").textContent = "v" + (d.dynamic.version ?? "?");
  document.getElementById("p-dyn").textContent = d.dynamic.content || "（无）";
}

setInterval(refreshStatus, 2000);
setInterval(refreshMessages, 5000);
refreshStatus(); refreshMessages(); refreshMemories(); refreshRules(); refreshPersona();
</script>
</body>
</html>
"""


class RuleIn(BaseModel):
    content: str
    trigger: str | None = None


class MemoryIn(BaseModel):
    content: str
    importance: int = 1


class RollbackIn(BaseModel):
    layer: str
    version: int


def create_app(cfg, storage, memory, presence) -> FastAPI:
    app = FastAPI(title="GGBot Panel")
    token = (cfg.web_cfg or {}).get("token", "")

    def _auth(x_token: str | None, t: str | None) -> None:
        if token and x_token != token and t != token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return INDEX_HTML

    @app.get("/api/status")
    async def status(x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        return presence.status()

    @app.get("/api/messages")
    async def messages(limit: int = 30, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        return storage.recent_messages_global(limit)

    @app.get("/api/memories")
    async def memories(limit: int = 20, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        return storage.list_memories(limit=limit)

    @app.get("/api/memories/search")
    async def memory_search(q: str, k: int = 5, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        return await asyncio.to_thread(memory.search, q, k)

    @app.post("/api/memories")
    async def memory_add(body: MemoryIn, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        mid = await asyncio.to_thread(memory.add, body.content, "fact", body.importance)
        return {"ok": True, "memory_id": mid}

    @app.post("/api/memories/tidy")
    async def memory_tidy(x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        removed = await asyncio.to_thread(memory.tidy)
        return {"ok": True, "removed": removed}

    @app.delete("/api/memories/{memory_id}")
    async def memory_delete(memory_id: str, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        storage.delete_memory(memory_id)
        return {"ok": True}

    @app.get("/api/rules")
    async def rules(x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        return storage.list_rules(active_only=False)

    @app.post("/api/rules")
    async def rule_add(body: RuleIn, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        rid = storage.add_rule(body.content, body.trigger)
        return {"ok": True, "rule_id": rid}

    @app.post("/api/rules/{rule_id}/toggle")
    async def rule_toggle(rule_id: int, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        row = next((r for r in storage.list_rules(active_only=False) if r["id"] == rule_id), None)
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        storage.set_rule_active(rule_id, not row["active"])
        return {"ok": True}

    @app.get("/api/persona")
    async def persona(x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        def _latest(layer: str) -> dict:
            versions = storage.list_persona_versions(layer, 1)
            return {"content": storage.get_persona(layer), "version": versions[0]["version"] if versions else 0,
                    "history": storage.list_persona_versions(layer, 10)}
        return {"inner": _latest("inner"), "dynamic": _latest("dynamic")}

    @app.post("/api/persona/rollback")
    async def persona_rollback(body: RollbackIn, x_token: str | None = Header(default=None), token_q: str | None = Query(default=None)):
        _auth(x_token, token_q)
        content = storage.rollback_persona(body.layer, body.version)
        if content is None:
            raise HTTPException(status_code=404, detail="version not found")
        return {"ok": True, "content": content}

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
    logger.info("Web 面板启动: http://%s:%s", host, port)
    await server.serve()
