"use strict";

/* ============ 令牌管理 ============ */
let TOKEN = localStorage.getItem("ggbot_token") || "";
const tokenBar = document.getElementById("token-bar");
const errBanner = document.getElementById("err-banner");

function showTokenBar(force) {
  tokenBar.classList.toggle("hidden", !(force || !TOKEN));
}

async function api(url, options) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {});
  if (TOKEN) headers["X-Token"] = TOKEN;
  const resp = await fetch(url, Object.assign({}, opts, { headers }));
  if (resp.status === 401) {
    TOKEN = "";
    localStorage.removeItem("ggbot_token");
    showTokenBar(true);
    showErr("令牌无效或缺失，请输入 config.json 中的 web.token");
    throw new Error("unauthorized");
  }
  return resp.json();
}

function showErr(msg) {
  errBanner.textContent = msg;
  errBanner.classList.remove("hidden");
  setTimeout(() => errBanner.classList.add("hidden"), 4000);
}

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[c]);
const fmtTs = ts => {
  if (!ts) return "—";
  const s = String(ts);
  return s.length >= 16 ? s.slice(5, 16) : s;   // MM-DD HH:MM:SS
};
const statusBadge = st => {
  if (st === "replied") return '<span class="badge ok">已回复</span>';
  if (st === "skipped") return '<span class="badge gray">跳过</span>';
  if (st === "failed") return '<span class="badge fail">失败</span>';
  if (st === "pending") return '<span class="badge warn">待处理</span>';
  return `<span class="badge gray">${esc(st || "—")}</span>`;
};

/* ============ 页面导航（后台分栏） ============ */
const PAGE_TITLES = {
  dash: "仪表盘", messages: "最近消息", memory: "向量记忆",
  rules: "规则", persona: "人设", settings: "设置", logs: "日志",
};

function switchPage(name) {
  document.querySelectorAll(".page").forEach(p => p.classList.add("hidden"));
  const page = document.getElementById("page-" + name);
  if (page) page.classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach(n =>
    n.classList.toggle("active", n.getAttribute("data-page") === name));
  document.getElementById("page-title").textContent = PAGE_TITLES[name] || name;
  document.getElementById("sidebar").classList.remove("open");
  if (name === "logs") loadLogs();
  if (name === "messages") refreshMessages();
}

/* ============ 运行状态 ============ */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const online = s.state === "online";
    const update = (id, txt, cls) => {
      const el = document.getElementById(id);
      if (el) { el.textContent = txt; if (cls) el.className = cls; }
    };
    update("st-state", online ? "在线" : "离线", "badge " + (online ? "on" : "off"));
    update("st-state2", online ? "在线" : "离线");
    update("st-pending-mini", (s.pending_count ?? "—") + " 待处理",
      "badge " + ((s.pending_count || 0) > 0 ? "warn" : "gray"));
    update("st-event", s.next_event || "—");
    update("st-remain", (s.remaining_sec != null ? s.remaining_sec.toFixed(0) : "—") + " s");
    update("st-pending", s.pending_count ?? "—");
    update("st-debounce", s.debounce_count ?? "—");
    update("st-dur",
      (s.online_duration_sec != null ? s.online_duration_sec + "s" : "—") + " / " +
      (s.offline_interval_sec != null ? s.offline_interval_sec + "s" : "—"));
    update("st-renew", s.renew_on_reply ? "开启（每发一条+30s）" : "关闭");
    update("st-backlog", (s.backlog_max_scan ?? "—") + " / " + (s.backlog_batch_size ?? "—"));
    // 会话级状态列表（每群/每私聊独立状态机）
    const slist = document.getElementById("st-sessions");
    if (slist) {
      const rows = (s.sessions || []).map(sess => {
        const on = sess.state === "online";
        return `<tr>
          <td class="mono muted">${esc(sess.label || sess.sid || "")}</td>
          <td><span class="badge ${on ? "on" : "off"}">${on ? "在线" : "离线"}</span></td>
          <td>${esc(sess.next_event || "—")}</td>
          <td>${sess.remaining_sec != null ? sess.remaining_sec.toFixed(0) + "s" : "—"}</td>
          <td>${sess.debounce_count || 0}</td>
        </tr>`;
      }).join("");
      slist.innerHTML = rows || `<tr><td colspan="5" class="muted">（暂无会话）</td></tr>`;
    }
    update("st-updated", new Date().toLocaleTimeString());
  } catch (e) { /* 401 已处理 */ }
}

/* ============ 最近消息 ============ */
async function refreshMessages() {
  try {
    const limit = document.getElementById("msg-limit").value;
    const d = await api("/api/messages?limit=" + limit);
    const rows = (d || []).map(m => {
      const roleCls = m.role === "assistant" ? "role-assistant" : "role-user";
      const content = (m.content || "").replace(/\s+/g, " ").slice(0, 80);
      return `<tr>
        <td class="mono muted">${esc(fmtTs(m.ts))}</td>
        <td class="skey" title="${esc(m.skey || "")}">${esc((m.skey || "").split(":")[0])}:${esc((m.skey || "").split(":")[1] || "").slice(0, 8)}</td>
        <td class="${roleCls}">${esc(m.role === "assistant" ? "机器人" : "成员")}</td>
        <td class="msg">${esc(content)}</td>
        <td>${statusBadge(m.status)}</td>
      </tr>`;
    }).join("");
    document.getElementById("msg-body").innerHTML =
      rows || '<tr><td colspan="5" class="empty">暂无消息</td></tr>';
  } catch (e) {}
}

/* ============ 向量记忆 ============ */
async function refreshMemories(q) {
  try {
    const url = q ? "/api/memories/search?q=" + encodeURIComponent(q) + "&k=8" : "/api/memories?limit=15";
    const d = await api(url);
    const rows = (d || []).map(m => `<tr>
      <td class="mono muted">${m.score != null ? m.score.toFixed(3) : "—"}</td>
      <td>${esc(m.content)}</td>
      <td><button class="btn ghost small" onclick="delMem('${esc(m.id)}')">删</button></td>
    </tr>`).join("");
    document.getElementById("mem-body").innerHTML =
      rows || '<tr><td colspan="3" class="empty">暂无记忆</td></tr>';
  } catch (e) {}
}

async function addMem() {
  const v = document.getElementById("mem-add").value.trim();
  if (!v) return;
  try {
    await api("/api/memories", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: v, importance: 1 }),
    });
    document.getElementById("mem-add").value = "";
    refreshMemories();
  } catch (e) {}
}

async function tidyMem() {
  try {
    const r = await api("/api/memories/tidy", { method: "POST" });
    showTmp("整理完成，合并删除 " + (r.removed || 0) + " 条");
    refreshMemories();
  } catch (e) {}
}

async function delMem(id) {
  try { await api("/api/memories/" + id, { method: "DELETE" }); refreshMemories(); } catch (e) {}
}

/* ============ 规则 ============ */
async function refreshRules() {
  try {
    const d = await api("/api/rules");
    const rows = (d || []).map(r => `<tr>
      <td>${esc(r.content)}</td>
      <td>${r.active ? '<span class="badge ok">启用</span>' : '<span class="badge gray">停用</span>'}</td>
      <td><button class="btn ghost small" onclick="toggleRule(${r.id})">切换</button></td>
    </tr>`).join("");
    document.getElementById("rule-body").innerHTML =
      rows || '<tr><td colspan="3" class="empty">暂无规则</td></tr>';
  } catch (e) {}
}

async function addRule() {
  const v = document.getElementById("rule-content").value.trim();
  if (!v) return;
  try {
    await api("/api/rules", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: v }),
    });
    document.getElementById("rule-content").value = "";
    refreshRules();
  } catch (e) {}
}

async function toggleRule(id) {
  try { await api("/api/rules/" + id + "/toggle", { method: "POST" }); refreshRules(); } catch (e) {}
}

/* ============ 人设 ============ */
async function refreshPersona() {
  try {
    const d = await api("/api/persona");
    const inner = d.inner || {}, dyn = d.dynamic || {};
    document.getElementById("p-inner-v").textContent = "v" + (inner.version ?? "?");
    document.getElementById("p-inner").textContent = inner.content || "（无）";
    document.getElementById("p-dyn-v").textContent = "v" + (dyn.version ?? "?");
    document.getElementById("p-dyn").textContent = dyn.content || "（无）";
    window._personaHistory = { inner: inner.history || [], dynamic: dyn.history || [] };
  } catch (e) {}
}

async function rollbackPersona(layer) {
  const hist = (window._personaHistory || {})[layer] || [];
  if (!hist.length) { alert("无历史版本可回滚"); return; }
  const pick = prompt(
    layer + " 历史版本（输入版本号回滚）：\n" +
    hist.map(h => `v${h.version}  ${h.ts}  ${(h.diff || "").slice(0, 30)}`).join("\n"),
    String(hist[0].version)
  );
  if (!pick) return;
  try {
    const r = await api("/api/persona/rollback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layer, version: parseInt(pick) }),
    });
    if (r.ok) { showTmp("已回滚 " + layer + " → v" + pick); refreshPersona(); }
  } catch (e) {}
}

/* ============ 设置 ============ */
const CHK = ["s-stream", "s-wake_on_at_message", "s-wake_on_c2c", "s-renew_on_reply"];
const NUM = ["s-temperature", "s-top_p", "s-max_tokens", "s-online_duration_sec", "s-offline_interval_sec",
  "s-max_segments", "s-max_chars_per_segment", "s-segment_gap_sec", "s-debounce_sec",
  "s-max_scan_messages", "s-batch_size"];

async function loadConfig() {
  try {
    const d = await api("/api/config");
    const c = d.config || {};
    const req = (c.deepseek || {}).request || {};
    const pr = (c.behavior || {}).presence || {};
    const rp = (c.behavior || {}).reply || {};
    const bk = (c.behavior || {}).backlog || {};
    const wb = (c.web || {});
    const vals = {
      "s-temperature": req.temperature, "s-top_p": req.top_p, "s-max_tokens": req.max_tokens,
      "s-thinking": req.thinking, "s-stream": req.stream,
      "s-online_duration_sec": pr.online_duration_sec, "s-offline_interval_sec": pr.offline_interval_sec,
      "s-wake_on_at_message": pr.wake_on_at_message, "s-wake_on_c2c": pr.wake_on_c2c,
      "s-renew_on_reply": pr.renew_on_reply,
      "s-max_segments": rp.max_segments, "s-max_chars_per_segment": rp.max_chars_per_segment,
      "s-segment_gap_sec": rp.segment_gap_sec, "s-debounce_sec": rp.debounce_sec,
      "s-max_scan_messages": bk.max_scan_messages, "s-batch_size": bk.batch_size,
      "s-priority_rule": bk.priority_rule, "s-log_level": wb.log_level,
    };
    for (const [id, v] of Object.entries(vals)) {
      const el = document.getElementById(id);
      if (!el) continue;
      if (CHK.includes(id)) el.checked = !!v;
      else if (v != null) el.value = v;
    }
  } catch (e) {}
}

const CFG_PATHS = {
  "s-temperature": "deepseek.request.temperature",
  "s-top_p": "deepseek.request.top_p",
  "s-max_tokens": "deepseek.request.max_tokens",
  "s-thinking": "deepseek.request.thinking",
  "s-stream": "deepseek.request.stream",
  "s-online_duration_sec": "behavior.presence.online_duration_sec",
  "s-offline_interval_sec": "behavior.presence.offline_interval_sec",
  "s-wake_on_at_message": "behavior.presence.wake_on_at_message",
  "s-wake_on_c2c": "behavior.presence.wake_on_c2c",
  "s-renew_on_reply": "behavior.presence.renew_on_reply",
  "s-max_segments": "behavior.reply.max_segments",
  "s-max_chars_per_segment": "behavior.reply.max_chars_per_segment",
  "s-segment_gap_sec": "behavior.reply.segment_gap_sec",
  "s-debounce_sec": "behavior.reply.debounce_sec",
  "s-max_scan_messages": "behavior.backlog.max_scan_messages",
  "s-batch_size": "behavior.backlog.batch_size",
  "s-priority_rule": "behavior.backlog.priority_rule",
  "s-log_level": "web.log_level",
};

async function saveConfig() {
  const items = Object.entries(CFG_PATHS).map(([id, path]) => {
    const el = document.getElementById(id);
    let value;
    if (CHK.includes(id)) value = el.checked;
    else if (NUM.includes(id)) value = parseFloat(el.value);
    else value = el.value;
    return { path, value };
  });
  try {
    const r = await api("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    const msg = document.getElementById("cfg-msg");
    msg.textContent = "已更新: " + (r.updated || []).length + " 项";
    setTimeout(() => { msg.textContent = ""; }, 4000);
  } catch (e) {}
}

/* ============ 日志 ============ */
async function loadLogs() {
  try {
    const level = document.getElementById("log-level").value;
    const d = await api("/api/logs?lines=300&level=" + level);
    const lines = d.lines || [];
    const view = document.getElementById("log-view");
    const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
    view.textContent = lines.length ? lines.join("\n") : "（无日志）";
    if (stick) view.scrollTop = view.scrollHeight;
  } catch (e) {}
}

/* ============ 杂项 ============ */
let _tmpTimer = null;
function showTmp(msg) {
  const el = document.getElementById("cfg-msg");
  el.textContent = msg;
  clearTimeout(_tmpTimer);
  _tmpTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

function bind() {
  document.getElementById("token-save").onclick = () => {
    const v = document.getElementById("token-input").value.trim();
    if (!v) return;
    TOKEN = v;
    localStorage.setItem("ggbot_token", v);
    tokenBar.classList.add("hidden");
    fullRefresh();
  };
  document.getElementById("btn-token").onclick = () => showTokenBar(true);
  document.getElementById("btn-refresh").onclick = fullRefresh;
  document.getElementById("btn-menu").onclick = () =>
    document.getElementById("sidebar").classList.toggle("open");
  document.querySelectorAll(".nav-item").forEach(n =>
    n.onclick = () => switchPage(n.getAttribute("data-page")));
  document.getElementById("btn-msg-refresh").onclick = refreshMessages;
  document.getElementById("msg-limit").onchange = refreshMessages;
  document.getElementById("btn-log-refresh").onclick = loadLogs;
  document.getElementById("log-level").onchange = loadLogs;
  document.getElementById("btn-tidy").onclick = tidyMem;
  document.getElementById("btn-mem-add").onclick = addMem;
  document.getElementById("btn-mem-search").onclick = () => refreshMemories(document.getElementById("mem-q").value.trim());
  document.getElementById("btn-mem-all").onclick = () => { document.getElementById("mem-q").value = ""; refreshMemories(); };
  document.getElementById("mem-add").onkeydown = e => { if (e.key === "Enter") addMem(); };
  document.getElementById("mem-q").onkeydown = e => { if (e.key === "Enter") refreshMemories(e.target.value.trim()); };
  document.getElementById("btn-rule-add").onclick = addRule;
  document.getElementById("rule-content").onkeydown = e => { if (e.key === "Enter") addRule(); };
  document.querySelectorAll("[data-rollback]").forEach(b => {
    b.onclick = () => rollbackPersona(b.getAttribute("data-rollback"));
  });
  document.getElementById("btn-save-cfg").onclick = saveConfig;
}

function fullRefresh() {
  refreshStatus(); refreshMessages(); refreshMemories(); refreshRules(); refreshPersona(); loadConfig(); loadLogs();
}

/* 启动 */
showTokenBar(false);
bind();
fullRefresh();
switchPage("dash");
setInterval(refreshStatus, 2000);
setInterval(refreshMessages, 5000);
setInterval(() => {
  if (document.getElementById("log-auto").checked) loadLogs();
}, 3000);
setInterval(() => { refreshMemories(); refreshRules(); refreshPersona(); }, 15000);
