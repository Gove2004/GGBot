"use strict";

/* ================= 工具 ================= */
const qs = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const PAGE_META = {
  dash: ["仪表盘", "运行概览与实时状态"],
  sessions: ["会话与上下文", "每个群 / 私聊的历史摘要与消息"],
  messages: ["消息流", "跨会话的最近消息记录"],
  memory: ["向量记忆", "长期记忆的语义检索与维护"],
  settings: ["设置", "配置热更新，写回 config.json"],
  permissions: ["权限", "发送者等级与工具调用门槛"],
  tools: ["工具", "一切皆 Tool · 独立配置 / 持久化数据 / 动态挂载"],
  logs: ["运行日志", "时间 | 等级 | 会话 | 内容"],
};

const REASON = {
  initial: "刚启动",
  scheduled: "自然上线",
  "wake-on-at": "被@唤醒",
  timeout: "自然下线",
  "reply-renew": "聊天续期",
  idle: "静默",
};

const state = {
  page: "dash",
  status: null,
  stats: null,
  sessions: [],
  selectedSession: null,
  memItems: [],
  memoryQuery: "",
  memoryUseSearch: false,
  loading: {},
  timers: [],
  confirmResolve: null,
  tools: [],
  toolCat: "",
  actors: [],
  levelNames: {},
  defaultLevel: 2,
  maxLevel: 5,
  lockedLevel: 6,
  logsRendered: false,
};

/* ================= API ================= */
async function api(path, options) {
  const opts = Object.assign({ credentials: "same-origin" }, options || {});
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    if (typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  }
  opts.headers = headers;
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (resp.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("登录已失效");
  }
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败（${resp.status}）`);
  }
  return data;
}

function guarded(name, fn) {
  if (state.loading[name]) return Promise.resolve();
  state.loading[name] = true;
  return Promise.resolve()
    .then(fn)
    .catch((err) => { if (err && err.message !== "登录已失效") toast(err.message, "error"); })
    .finally(() => { state.loading[name] = false; });
}

/* ================= 提示 / 弹窗 ================= */
let lastErrorToast = { msg: "", at: 0 };
function toast(msg, type) {
  if (type === "error") {
    const now = Date.now();
    if (lastErrorToast.msg === msg && now - lastErrorToast.at < 4000) return;
    lastErrorToast = { msg, at: now };
  }
  const stack = qs("#toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${type || "info"}`;
  el.innerHTML = `<span class="toast-ico">${type === "success" ? "✓" : type === "error" ? "!" : "i"}</span>
    <div class="toast-body">${esc(msg)}</div>
    <button class="toast-x" type="button" aria-label="关闭">×</button>
    <span class="toast-bar"></span>`;
  el.querySelector(".toast-x").addEventListener("click", () => el.remove());
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function openModal(title, body, foot) {
  qs("#modal-title").textContent = title;
  qs("#modal-body").innerHTML = body || "";
  qs("#modal-foot").innerHTML = foot || "";
  qs("#modal-root").classList.remove("hidden");
}

function closeModal(resolveValue) {
  qs("#modal-root").classList.add("hidden");
  const r = state.confirmResolve;
  state.confirmResolve = null;
  if (r && resolveValue !== undefined) r(resolveValue);
}

function confirmAction(title, text, okLabel) {
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
    openModal(
      title,
      `<p class="muted">${esc(text)}</p>`,
      `<button class="btn ghost" data-action="confirm-no">取消</button>
       <button class="btn danger" data-action="confirm-yes">${esc(okLabel || "确定")}</button>`
    );
  });
}

/* ================= 登录 ================= */
function showLogin() {
  stopPolling();
  closeModal(false);
  qs("#app").classList.add("hidden");
  qs("#login").classList.remove("hidden");
  const input = qs("#login-token");
  input.value = "";
  setTimeout(() => input.focus(), 50);
}

function showLoginError(msg) {
  const el = qs("#login-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function showApp() {
  qs("#login").classList.add("hidden");
  qs("#login-error").classList.add("hidden");
  qs("#app").classList.remove("hidden");
  const initial = location.hash.slice(1);
  if (PAGE_META[initial]) state.page = initial;
  startPolling();
  switchPage(state.page || "dash");
  refreshAll();
}

async function boot() {
  try {
    const a = await api("/api/auth");
    if (a.authenticated) showApp();
    else showLogin();
  } catch (_) {
    showLogin();
  }
}

async function doLogin(event) {
  event.preventDefault();
  const input = qs("#login-token");
  const token = input.value.trim();
  if (!token) return;
  qs("#login-error").classList.add("hidden");
  try {
    await api("/api/login", { method: "POST", body: { token } });
    input.value = "";
    showApp();
    toast("已登录", "success");
  } catch (err) {
    showLoginError(err.message);
  }
}

async function doLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  showLogin();
  toast("已退出登录", "info");
}

/* ================= 轮询 ================= */
function startPolling() {
  stopPolling();
  state.timers.push(setInterval(() => {
    if (!document.hidden) guarded("status", refreshStatus);
  }, 1000));
  state.timers.push(setInterval(() => {
    if (document.hidden) return;
    guarded("stats", refreshStats);
    guarded("sessions", refreshSessions);
    if (state.page === "messages") guarded("messages", refreshMessages);
    if (state.page === "dash") guarded("recent", refreshDashRecent);
  }, 1000));
  state.timers.push(setInterval(() => {
    if (document.hidden || state.page !== "logs") return;
    if (!qs("#log-auto").checked) return;
    guarded("logs", refreshLogs);
  }, 1000));
}

function stopPolling() {
  state.timers.forEach((t) => clearInterval(t));
  state.timers = [];
}

/* ================= 格式化 ================= */
function fmtTs(ts) {
  const s = String(ts || "");
  if (!s) return "—";
  return s.length >= 16 ? s.slice(5, 16) : s;
}
function fmtLogTs(ts) {
  const s = String(ts || "");
  return s.length >= 19 ? s.slice(5, 19) : (s || "—");
}
function shortId(id) {
  const s = String(id || "");
  return s ? s.slice(0, 8) : "—";
}
function senderCell(m) {
  if (m.role === "assistant") return '<span class="role-assistant">机器人</span>';
  const name = (m.sender_name || "").trim();
  const head = name ? esc(name) : `<span class="mono">${esc(shortId(m.sender))}</span>`;
  const tail = name ? ` <span class="mono muted">${esc(shortId(m.sender))}</span>` : "";
  return `<span class="role-user">${head}${tail}</span>`;
}
function fmtAgo(sec) {
  if (sec == null) return "—";
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return "—";
  if (n < 60) return `${Math.round(n)}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m`;
  return `${Math.floor(n / 3600)}h`;
}
function fmtUptime(sec) {
  const n = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(n / 86400);
  const h = Math.floor((n % 86400) / 3600);
  const m = Math.floor((n % 3600) / 60);
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  if (m) return `${m}分${n % 60}秒`;
  return `${n}秒`;
}
function fmtNum(v) {
  if (v == null || !isFinite(Number(v))) return "—";
  return Number(v).toLocaleString("en-US");
}
function labelFromKey(key) {
  const s = String(key || "");
  const i = s.indexOf(":");
  const prefix = i < 0 ? "" : s.slice(0, i);
  const rest = i < 0 ? s : s.slice(i + 1);
  if (prefix === "group") return `群聊${rest.slice(0, 8)}`;
  if (prefix === "c2c") return `私聊${rest.slice(0, 8)}`;
  const raw = rest.slice(0, 8);
  return raw && raw.toLowerCase() !== "system" ? `系统${raw}` : "系统00000000";
}
function reasonLabel(reason) {
  const m = /^(\S+)\s+x(\d+)$/.exec(reason || "");
  if (m && REASON[m[1]]) return `${REASON[m[1]]} ×${m[2]}`;
  return REASON[reason] || reason || "—";
}
function statusBadge(status) {
  if (status === "sent") return '<span class="badge ok">已发送</span>';
  if (status === "replied") return '<span class="badge ok">已回复</span>';
  if (status === "skipped") return '<span class="badge gray">跳过</span>';
  if (status === "failed") return '<span class="badge fail">失败</span>';
  if (status === "seen") return '<span class="badge gray">已读</span>';
  if (status === "pending") return '<span class="badge warn">待处理</span>';
  if (status === "received") return '<span class="badge gray">已接收</span>';
  return `<span class="badge gray">${esc(status || "—")}</span>`;
}
function stateBadge(st) {
  const on = st === "online";
  return `<span class="badge ${on ? "on" : "off"}">${on ? "在线" : "离线"}</span>`;
}
function sessKind(key) {
  const p = String(key || "").split(":")[0];
  return p === "group" ? "group" : p === "c2c" ? "c2c" : "sys";
}
function sessChip(key, extra) {
  return `<span class="sess-chip ${sessKind(key)}${extra ? " " + extra : ""}">${esc(labelFromKey(key))}</span>`;
}
function logSessChip(label) {
  const t = String(label || "—");
  const kind = t.startsWith("群聊") ? "group" : t.startsWith("私聊") ? "c2c" : "sys";
  return `<span class="sess-chip ${kind} tiny">${esc(t)}</span>`;
}

/* ================= 页面切换 ================= */
function switchPage(name) {
  if (!PAGE_META[name]) name = "dash";
  state.page = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  qsa(".page").forEach((p) => p.classList.add("hidden"));
  const page = qs(`#page-${name}`);
  if (page) page.classList.remove("hidden");
  qsa(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === name));
  const meta = PAGE_META[name];
  qs("#page-title").textContent = meta[0];
  qs("#page-sub").textContent = meta[1];
  closeSidebar();
  if (name === "dash") {
    guarded("status", refreshStatus);
    guarded("stats", refreshStats);
    guarded("sessions", refreshSessions);
    guarded("recent", refreshDashRecent);
  } else if (name === "sessions") {
    guarded("sessions", refreshSessions);
  } else if (name === "messages") {
    guarded("sessions", refreshSessions);
    guarded("messages", refreshMessages);
  } else if (name === "memory") {
    guarded("sessions", refreshSessions);
    guarded("memories", refreshMemories);
  } else if (name === "settings") {
    guarded("config", loadConfig);
  } else if (name === "permissions") {
    guarded("permissions", refreshPermissions);
  } else if (name === "tools") {
    guarded("tools", refreshTools);
  } else if (name === "logs") {
    state.logsRendered = false;
    guarded("logs", refreshLogs);
  }
}

function closeSidebar() {
  qs("#sidebar").classList.remove("open");
  qs("#side-overlay").classList.add("hidden");
}

function setRail(on) {
  qs("#app").classList.toggle("rail", !!on);
  qsa(".nav-item").forEach((n) => {
    const t = n.querySelector("span");
    if (t) n.title = t.textContent;
  });
  try { localStorage.setItem("ggbot.rail", on ? "1" : "0"); } catch (_) { /* ignore */ }
}

/* ================= 状态 / 统计 ================= */
async function refreshStatus() {
  const s = await api("/api/status");
  state.status = s;
  const online = s.state === "online";
  const conn = qs("#conn-pill");
  conn.className = `pill ${online ? "on" : "off"}`;
  conn.innerHTML = `<i></i>${online ? "在线" : "离线"}`;
  const pending = s.pending_count || 0;
  const pend = qs("#pending-pill");
  pend.className = `pill ${pending > 0 ? "warn" : "ghost"}`;
  pend.textContent = `${pending} 待处理`;

  qs("#side-dot").className = `dot${online ? "" : " off"}`;
  qs("#side-state").textContent = online ? "在线" : "离线";
  qs("#side-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#side-pending").textContent = pending;

  qs("#dash-hero").classList.toggle("online", online);
  qs("#dash-dot").className = `state-dot ${online ? "on" : "off"}`;
  qs("#dash-state").textContent = online ? "在线" : "离线";
  qs("#dash-event").textContent = s.next_event || "等待下一次上线";
  qs("#dash-remain").textContent = s.remaining_sec != null ? Math.max(0, Math.round(s.remaining_sec)) : "—";
  const base = Number(online ? s.online_duration_sec : s.offline_interval_sec) || 1;
  const remain = Math.max(0, Number(s.remaining_sec) || 0);
  const prog = qs("#dash-progress");
  prog.max = Math.max(1, Math.round(base));
  prog.value = Math.max(0, Math.min(base, base - remain));
  const live = (s.sessions && s.sessions[0]) || null;
  qs("#dash-caption").textContent =
    `${reasonLabel(live ? live.reason : "idle")} · ${s.online_sessions || 0} 个会话在线 · ${pending} 条积压`;

  qs("#m-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#m-pending").textContent = s.pending_count != null ? s.pending_count : "—";
  renderDashSessions();
}

async function refreshStats() {
  const d = await api("/api/stats");
  state.stats = d;
  qs("#m-online").textContent = d.online_sessions != null ? d.online_sessions : "—";
  qs("#m-pending").textContent = d.pending != null ? d.pending : "—";
  qs("#m-memories").textContent = d.memories != null ? d.memories : "—";
  qs("#m-sessions").textContent = d.sessions != null ? d.sessions : "—";
  qs("#m-today").textContent = d.messages_today != null ? d.messages_today : "—";
  const rt = d.runtime || {};
  const uptime = qs("#rt-uptime");
  if (uptime) uptime.textContent = rt.uptime_sec != null ? `运行 ${fmtUptime(rt.uptime_sec)}` : "运行时长 —";
  qs("#rt-sessions").textContent = fmtNum(rt.sessions);
  qs("#rt-received").textContent = fmtNum(rt.received);
  qs("#rt-sent").textContent = fmtNum(rt.sent);
  qs("#rt-tools").textContent = fmtNum(rt.tool_calls);
  qs("#rt-llm").textContent = fmtNum(rt.llm_calls);
  qs("#rt-vision").textContent = fmtNum(rt.vision_calls);
  qs("#rt-tokens").textContent = fmtNum(rt.tokens);
  qs("#rt-prompt").textContent = fmtNum(rt.prompt_tokens);
  qs("#rt-completion").textContent = fmtNum(rt.completion_tokens);
  const promptToks = Number(rt.prompt_tokens) || 0;
  const cachedToks = Number(rt.cached_tokens) || 0;
  const cache = qs("#rt-cache");
  if (cache) {
    cache.textContent = promptToks > 0 ? `${Math.round((cachedToks / promptToks) * 100)}%` : "—";
    cache.title = `缓存命中 ${fmtNum(cachedToks)} / 输入 ${fmtNum(promptToks)} tokens`;
  }
  const priced = rt.cost_text && rt.cost_text !== "-";
  qs("#rt-cost").textContent = priced ? rt.cost_text : "—";
  const hint = qs("#rt-price-hint");
  if (hint) hint.textContent = priced ? "按 llm.pricing 单价估算" : "未配置单价 · 见设置页";
  renderEnv(d.env);
}

function renderEnv(env) {
  if (!env) return;
  const side = qs("#side-model");
  if (side) {
    side.textContent = env.chat_model || "配置文件驱动";
    side.title = `${env.chat_model} @ ${env.chat_host}`;
  }
  const set = (id, text, title) => {
    const el = qs(id);
    if (!el) return;
    el.textContent = text == null ? "—" : String(text);
    if (title !== undefined) el.title = title;
  };
  set("#env-chat", env.chat_model, env.chat_host);
  set("#env-chat-host", env.chat_host);
  set("#env-vision", env.vision_model || "未启用");
  set("#env-vision-host", env.vision_model ? env.vision_host : "vision.enabled=false 或未填模型");
  set("#env-memory", env.memory_mode === "向量" ? "向量检索" : "关键词检索");
  set("#env-memory-model", env.memory_model || "未配置向量服务，记忆改用关键词检索");
  set("#env-log", env.log_level);
  set("#env-log-file", `${env.temp}/logs/ggbot.log`);
  set("#env-db", env.db);
  set("#env-temp", env.temp);
}

function renderDashSessions() {
  const body = qs("#dash-sessions");
  if (!body) return;
  const live = {};
  ((state.status && state.status.sessions) || []).forEach((x) => { live[x.sid] = x; });
  const row = (sid, l, pending) => `<tr>
      <td>${sessChip(sid)}</td>
      <td>${stateBadge(l.state)}<span class="cell-note">${esc(reasonLabel(l.reason))}</span></td>
      <td class="col-num ${pending ? "warn" : "muted"}">${pending == null ? "—" : pending}</td>
      <td class="col-num mono">${l.remaining_sec != null ? Math.max(0, Math.round(l.remaining_sec)) + "s" : "—"}</td>
      <td class="muted small">${fmtAgo(l.idle_sec)}</td>
    </tr>`;
  let rows = state.sessions.slice(0, 8).map((s) => {
    const l = (s.live && s.live.state) ? s.live : (live[s.id] || { state: "offline", reason: "idle", remaining_sec: 0, idle_sec: null });
    return row(s.id, l, s.pending_count || 0);
  });
  if (!rows.length) {
    rows = ((state.status && state.status.sessions) || []).slice(0, 8).map((l) => row(l.sid, l, null));
  }
  body.innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">暂无会话</td></tr>';
}

/* ================= 会话 ================= */
async function refreshSessions() {
  const rows = await api("/api/sessions?limit=500");
  state.sessions = Array.isArray(rows) ? rows : [];
  renderSessions();
  populateSessionSelects();
  renderDashSessions();
  if (state.page === "sessions") {
    const still = state.selectedSession && state.sessions.some((s) => s.id === state.selectedSession);
    if (!still) {
      const first = state.sessions[0];
      if (first) openSessionDetail(first.id);
      else {
        state.selectedSession = null;
        const pane = qs("#session-detail");
        if (pane) pane.innerHTML = detailEmptyHTML();
      }
    }
  }
}

function renderSessions() {
  const list = qs("#sessions-list");
  if (!list) return;
  const q = (qs("#sess-q").value || "").trim().toLowerCase();
  const rows = state.sessions.filter((s) => {
    if (!q) return true;
    return [s.label, s.id, s.summary, s.last_message].some((v) => String(v || "").toLowerCase().includes(q));
  });
  const top = list.scrollTop;
  list.innerHTML = rows.map((s) => {
    const l = s.live || { state: "offline", reason: "idle", remaining_sec: 0, idle_sec: null };
    const preview = s.summary || s.last_message || "—";
    return `<button class="session-item${s.id === state.selectedSession ? " active" : ""}" data-action="session-view" data-sid="${esc(s.id)}">
      <div class="session-top">${sessChip(s.id)}${stateBadge(l.state)}</div>
      <div class="session-preview">${esc(preview)}</div>
      <div class="session-meta">
        <span>${s.message_count || 0} 条</span>
        <span class="${s.pending_count ? "warn" : ""}">${s.pending_count || 0} 待处理</span>
        <span>${esc(fmtTs(s.last_ts || s.updated_at))}</span>
      </div>
    </button>`;
  }).join("");
  if (!rows.length) list.innerHTML = '<div class="empty">暂无会话</div>';
  list.scrollTop = top;
}

function populateSessionSelects() {
  const opts = state.sessions.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.label || labelFromKey(s.id))} · ${esc(String(s.id).split(":")[1] || "").slice(0, 8)}</option>`
  ).join("");
  [["#msg-session", "全部会话"], ["#mem-session", "全部作用域"]].forEach(([sel, label]) => {
    const el = qs(sel);
    if (!el) return;
    const cur = el.value;
    el.innerHTML = `<option value="">${label}</option>${opts}`;
    if (cur && Array.from(el.options).some((o) => o.value === cur)) el.value = cur;
  });
}

function detailEmptyHTML() {
  return `<div class="detail-empty">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H8l-4 4V5Z"/></svg>
    <p>选择左侧会话查看上下文</p>
  </div>`;
}

async function openSessionDetail(sid) {
  if (!sid) return;
  state.selectedSession = sid;
  renderSessions();
  const pane = qs("#session-detail");
  if (!pane) return;
  pane.innerHTML = '<div class="empty detail-loading">加载中…</div>';
  try {
    const d = await api(`/api/sessions/${encodeURIComponent(sid)}/messages?limit=100`);
    if (state.selectedSession !== sid) return;
    const s = d.session || {};
    const summary = d.summary
      ? `<div class="modal-section"><h3>历史摘要</h3><div class="summary-box">${esc(d.summary)}</div></div>`
      : "";
    const chats = (d.messages || []).map((m) => {
      const role = m.role === "assistant" ? "assistant" : "user";
      const pending = m.status === "pending" ? " · 待处理" : "";
      return `<div class="chat ${role}"><div class="chat-meta">${senderCell(m)} · ${esc(fmtTs(m.ts))}${pending}</div>${esc(m.content || "（无文本）")}</div>`;
    }).join("");
    const liveS = ((state.status && state.status.sessions) || []).find((x) => x.sid === sid);
    const stateCell = liveS
      ? `${stateBadge(liveS.state)}<span class="cell-note">${esc(reasonLabel(liveS.reason))} · 剩 ${Math.max(0, Math.round(liveS.remaining_sec || 0))}s</span>`
      : `${stateBadge("offline")}<span class="cell-note">静默</span>`;
    const summaryLen = (d.summary || "").length;
    pane.innerHTML = `<div class="detail-inner">
      <div class="detail-head">
        <div>
          <div class="detail-title">${sessChip(s.id || sid)}</div>
          <div class="sid-line">${esc(s.id || sid)}</div>
        </div>
        <div class="row-actions">
          <button class="btn danger small" data-action="session-clear" data-sid="${esc(s.id || sid)}">清空上下文</button>
        </div>
      </div>
      <div class="detail-stats">
        <span class="stat-chip">消息 <b>${s.message_count || 0}</b></span>
        <span class="stat-chip">待处理 <b>${s.pending_count || 0}</b></span>
        <span class="stat-chip">历史摘要 <b>${summaryLen ? `${summaryLen} 字` : "无"}</b></span>
        <span class="stat-chip">最近发言 <b>${esc(fmtTs(s.last_ts || s.updated_at))}</b></span>
        <span class="stat-chip sess-state">作息 ${stateCell}</span>
      </div>
      <div class="detail-scroll">
        ${summary}
        <div class="modal-section"><h3>最近消息</h3><div class="chat-list">${chats || '<div class="empty">暂无消息</div>'}</div></div>
      </div>
    </div>`;
  } catch (err) {
    if (state.selectedSession !== sid) return;
    pane.innerHTML = `<div class="detail-empty"><p>${esc(err.message)}</p></div>`;
  }
}

async function clearSession(sid) {
  const ok = await confirmAction("清空上下文", `将删除该会话的全部消息与历史摘要，长期记忆不受影响。`, "清空");
  if (!ok) return;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(sid)}/context`, { method: "DELETE" });
    toast(`已清空 ${r.messages || 0} 条消息`, "success");
    await refreshSessions();
    openSessionDetail(sid);
    refreshStatus();
  } catch (err) { toast(err.message, "error"); }
}

/* ================= 消息 ================= */
async function refreshMessages() {
  const params = new URLSearchParams();
  params.set("limit", qs("#msg-limit").value || "30");
  const sid = qs("#msg-session").value;
  const role = qs("#msg-role").value;
  if (sid) params.set("session_id", sid);
  if (role) params.set("role", role);
  const rows = await api(`/api/messages?${params}`);
  const body = qs("#msg-body");
  body.innerHTML = (rows || []).map((m) => {
    const role = m.role === "assistant" ? "assistant" : "user";
    return `<article class="stream-item ${role}">
      <div class="stream-card">
        <div class="stream-top">
          ${senderCell(m)}
          ${sessChip(m.skey || m.session_id, "tiny")}
          <span class="stream-ts muted">${esc(fmtTs(m.ts))}</span>
          <span class="stream-status">${statusBadge(m.status)}</span>
        </div>
        <div class="stream-text">${esc(m.content || "（无文本）")}</div>
      </div>
    </article>`;
  }).join("");
  if (!rows || !rows.length) body.innerHTML = '<div class="empty">暂无消息</div>';
}

async function refreshDashRecent() {
  const rows = await api("/api/messages?limit=8");
  const box = qs("#dash-recent");
  box.innerHTML = (rows || []).map((m) => `<div class="mini-item">
    <span class="mini-role ${m.role === "assistant" ? "assistant" : ""}"></span>
    <div class="mini-main">
      <div class="mini-meta"><span>${sessChip(m.skey || m.session_id, "tiny")}</span><span>${esc(fmtTs(m.ts))}</span></div>
      <div class="mini-text" title="${esc(m.content)}">${esc(m.content || "（无文本）")}</div>
    </div>
  </div>`).join("") || '<div class="empty">暂无消息</div>';
}

/* ================= 记忆 ================= */
async function refreshMemories() {
  const sid = qs("#mem-session").value;
  if (state.memoryUseSearch && state.memoryQuery) {
    const params = new URLSearchParams({ q: state.memoryQuery, k: "20" });
    if (sid) params.set("session_id", sid);
    const items = await api(`/api/memories/search?${params}`);
    renderMemories(items, null, true);
  } else {
    const params = new URLSearchParams({ limit: "100", offset: "0" });
    if (sid) params.set("session_id", sid);
    const d = await api(`/api/memories?${params}`);
    renderMemories(d.items || [], d.total, false);
  }
}

function renderMemories(items, total, isSearch) {
  state.memItems = items || [];
  qs("#mem-total").textContent = isSearch
    ? `检索 “${state.memoryQuery}” · ${state.memItems.length} 条结果`
    : `长期记忆 · 共 ${total == null ? state.memItems.length : total} 条`;
  const body = qs("#mem-body");
  body.innerHTML = state.memItems.map((m) => {
    const scope = m.session_id ? sessChip(m.session_id, "tiny") : '<span class="badge gray">全局</span>';
    const score = m.score != null ? `<div class="score">${Number(m.score).toFixed(3)}</div>` : "";
    return `<tr>
      <td><span class="stars">${"★".repeat(Math.max(1, Math.min(5, Number(m.importance) || 1)))}</span>${score}</td>
      <td>${esc(m.content)}</td>
      <td class="col-sm-hide">${scope}</td>
      <td class="muted small col-sm-hide">${esc(fmtTs(m.ts))}</td>
      <td class="col-actions"><div class="row-actions">
        <button class="btn ghost small" data-action="memory-edit" data-id="${esc(m.id)}">编辑</button>
        <button class="btn danger small" data-action="memory-delete" data-id="${esc(m.id)}">删</button>
      </div></td>
    </tr>`;
  }).join("");
  if (!state.memItems.length) body.innerHTML = '<tr><td colspan="5" class="empty">暂无记忆</td></tr>';
}

async function addMemory() {
  const input = qs("#mem-add");
  const content = input.value.trim();
  if (!content) return;
  const sid = qs("#mem-session").value || null;
  try {
    await api("/api/memories", {
      method: "POST",
      body: { content, importance: Number(qs("#mem-importance").value), session_id: sid },
    });
    input.value = "";
    toast("记忆已写入", "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

async function searchMemories() {
  state.memoryQuery = qs("#mem-q").value.trim();
  state.memoryUseSearch = !!state.memoryQuery;
  if (!state.memoryQuery) { refreshMemories(); return; }
  await guarded("memories", refreshMemories);
}

function openMemoryEdit(id) {
  const item = state.memItems.find((x) => x.id === id);
  if (!item) return;
  const imp = Math.max(1, Math.min(5, Number(item.importance) || 1));
  openModal(
    "编辑记忆",
    `<label class="field"><span>内容</span><textarea id="mem-edit-content">${esc(item.content)}</textarea></label>
     <label class="field"><span>重要度</span><select id="mem-edit-importance" class="sel">
       <option value="1">★</option><option value="2">★★</option><option value="3">★★★</option>
       <option value="4">★★★★</option><option value="5">★★★★★</option>
     </select></label>`,
    `<button class="btn ghost" data-action="modal-close">取消</button>
     <button class="btn primary" data-action="memory-save" data-id="${esc(id)}">保存</button>`
  );
  qs("#mem-edit-importance").value = String(imp);
}

async function saveMemory(id) {
  const content = qs("#mem-edit-content").value.trim();
  if (!content) { toast("内容不能为空", "error"); return; }
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: { content, importance: Number(qs("#mem-edit-importance").value) },
    });
    closeModal();
    toast("记忆已更新", "success");
    refreshMemories();
  } catch (err) { toast(err.message, "error"); }
}

async function deleteMemory(id) {
  const ok = await confirmAction("删除记忆", "删除后无法恢复，确定继续？", "删除");
  if (!ok) return;
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("记忆已删除", "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

async function tidyMemory() {
  const ok = await confirmAction("合并重复记忆", "将合并相似度 ≥ 0.95 的记忆条目，保留更重要的一条。", "开始整理");
  if (!ok) return;
  try {
    const r = await api("/api/memories/tidy", { method: "POST" });
    toast(`整理完成，合并删除 ${r.removed || 0} 条`, "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

/* ================= 设置 ================= */
function getPath(obj, path) {
  return path.split(".").reduce((node, key) => (node && typeof node === "object" ? node[key] : undefined), obj);
}

function setConfigDirty(dirty) {
  const bar = qs(".save-bar");
  if (!bar) return;
  bar.classList.toggle("dirty", !!dirty);
  if (dirty) qs("#cfg-msg").textContent = "有未保存的修改，点击右侧保存";
}

async function loadConfig() {
  const d = await api("/api/config");
  qsa("[data-path]").forEach((el) => {
    const v = getPath(d.config, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else if (v !== undefined && v !== null) el.value = v;
  });
  setConfigDirty(false);
  qs("#cfg-msg").textContent = "修改后点击保存，写回 config.json 并热生效";
}

async function saveConfig() {
  const items = [];
  let bad = null;
  qsa("[data-path]").forEach((el) => {
    let value;
    const numeric = el.dataset.kind === "int" || el.dataset.kind === "float";
    if (el.type === "checkbox") value = el.checked;
    else if (numeric && el.value.trim() === "") return; // 留空表示不修改
    else if (el.dataset.kind === "int") value = parseInt(el.value, 10);
    else if (el.dataset.kind === "float") value = parseFloat(el.value);
    else value = el.value;
    if (numeric && !isFinite(value)) {
      bad = el.closest(".field")?.querySelector("span")?.textContent || el.dataset.path;
    }
    items.push({ path: el.dataset.path, value });
  });
  if (bad) { toast(`「${bad}」不是有效数字`, "error"); return; }
  try {
    const r = await api("/api/config", { method: "POST", body: items });
    const rejected = (r.rejected || []).length;
    setConfigDirty(false);
    qs("#cfg-msg").textContent = `已保存 ${(r.updated || []).length} 项${rejected ? `，拒绝 ${rejected} 项` : ""}`;
    toast(`设置已保存${rejected ? `（${rejected} 项无效）` : ""}`, rejected ? "info" : "success");
    if (r.config) {
      qsa("[data-path]").forEach((el) => {
        const v = getPath(r.config, el.dataset.path);
        if (el.type === "checkbox") el.checked = !!v;
        else if (v !== undefined && v !== null) el.value = v;
      });
    }
  } catch (err) {
    toast(err.message, "error");
    setConfigDirty(true);
  }
}

/* ================= 工具 ================= */
const TOOL_CATS = { chat: "对话", knowledge: "信息", fs: "文件", mail: "邮件", admin: "管理" };
const TOOL_CAT_ORDER = ["chat", "knowledge", "fs", "mail", "admin"];
function toolCat(cat) { return TOOL_CATS[cat] || cat || "其他"; }
function toolOrder(cat) {
  const i = TOOL_CAT_ORDER.indexOf(cat);
  return i < 0 ? 99 : i;
}
function fmtCfgValue(v) {
  if (Array.isArray(v)) return v.length > 6 ? `[${v.length} 项]` : v.map(fmtCfgValue).join(",");
  if (v && typeof v === "object") return JSON.stringify(v);
  return String(v);
}
async function fetchTools() {
  const d = await api("/api/tools");
  state.tools = (d.tools || []).slice().sort((a, b) =>
    toolOrder(a.category) - toolOrder(b.category) || a.name.localeCompare(b.name));
}
async function refreshTools() {
  await fetchTools();
  renderToolCats();
  renderTools();
}
function renderToolCats() {
  const tools = state.tools || [];
  const counts = {};
  tools.forEach((t) => { counts[t.category] = (counts[t.category] || 0) + 1; });
  const cats = Object.keys(counts).sort((a, b) => toolOrder(a) - toolOrder(b));
  const chips = [`<button class="chip ${state.toolCat ? "" : "active"}" data-action="tool-cat" data-cat="">全部<span>${tools.length}</span></button>`]
    .concat(cats.map((c) =>
      `<button class="chip ${state.toolCat === c ? "active" : ""}" data-action="tool-cat" data-cat="${esc(c)}">${esc(toolCat(c))}<span>${counts[c]}</span></button>`));
  qs("#tool-cats").innerHTML = chips.join("");
}
function renderTools() {
  const tools = (state.tools || []).filter((t) => !state.toolCat || t.category === state.toolCat);
  const rows = tools.map((t) => {
    const cfg = Object.entries(t.config || {})
      .filter(([k]) => k !== "enabled")
      .map(([k, v]) => `${k}=${fmtCfgValue(v)}`)
      .join(" · ");
    const lv = levelLabel(t.min_level);
    const adjusted = t.level_default != null && Number(t.min_level) !== Number(t.level_default);
    const lvTitle = adjusted ? `代码默认 L${t.level_default}，已在权限页调整` : "门槛与代码默认一致";
    return `<div class="tool-card ${t.enabled ? "" : "off"}" data-cat="${esc(t.category || "")}">
      <div class="tool-head">
        <div><div class="tool-name"><span class="tool-state ${t.enabled ? "on" : ""}"></span>${esc(t.name)} <span class="badge gray">${esc(toolCat(t.category))}</span> <span class="badge ${adjusted ? "warn" : "gray"}" title="${esc(lvTitle)}">${esc(lv)}${adjusted ? " ↕" : ""}</span></div>
        <div class="tool-desc muted small">${esc(t.description)}</div></div>
        <button class="btn ${t.enabled ? "ghost" : "primary"} small"
          data-action="tool-toggle" data-tool="${esc(t.name)}" data-enabled="${t.enabled ? 1 : 0}"
          ${t.toggleable === false ? "disabled title=\"随同组工具启停\"" : ""}>
          ${t.toggleable === false ? "同组" : t.enabled ? "停用" : "启用"}
        </button>
      </div>
      ${cfg ? `<div class="tool-meta">${esc(cfg)}</div>` : ""}
    </div>`;
  }).join("");
  qs("#tool-list").innerHTML = rows || `<p class="empty">${state.toolCat ? "该分类下暂无工具" : "（无工具）"}</p>`;
}

/* ================= 权限 ================= */
const LEVEL_FALLBACK_NAMES = { 0: "禁止", 1: "受限", 2: "普通", 3: "特权", 4: "心腹", 5: "归属" };
function levelLabel(n) {
  const num = Number(n);
  if (num >= state.lockedLevel) return "永久锁";
  const name = (state.levelNames && state.levelNames[num]) || LEVEL_FALLBACK_NAMES[num] || "";
  return `L${num} ${name}`.trim();
}
function levelOptions(selected, lo, hi) {
  const out = [];
  for (let i = lo; i <= hi; i++) {
    out.push(`<option value="${i}"${Number(selected) === i ? " selected" : ""}>${esc(levelLabel(i))}</option>`);
  }
  return out.join("");
}
async function refreshPermissions() {
  const d = await api("/api/actors");
  state.actors = d.actors || [];
  state.levelNames = d.level_names || {};
  state.defaultLevel = d.default_level != null ? d.default_level : 2;
  state.maxLevel = d.max_level != null ? d.max_level : 5;
  state.lockedLevel = d.locked_level != null ? d.locked_level : 6;
  const sub = qs("#perm-sub");
  if (sub) {
    sub.textContent = `发送者等级决定它能触发哪些工具 · 未登记成员默认 ${levelLabel(state.defaultLevel)}`;
  }
  const addSel = qs("#actor-level");
  if (addSel) addSel.innerHTML = levelOptions(state.defaultLevel, 0, state.maxLevel);
  renderActors();
  await fetchTools();
  renderPermTools();
}
function renderActors() {
  const body = qs("#actor-body");
  if (!body) return;
  const rows = (state.actors || []).map((a) => `<tr>
    <td>
      <div>${esc(a.name || "—")}${a.explicit ? ' <span class="badge gray">已设置</span>' : ""}</div>
      <div class="mono small muted">${esc(a.openid)}</div>
    </td>
    <td><select class="sel small-input" data-actor-level data-openid="${esc(a.openid)}">${levelOptions(a.level, 0, state.maxLevel)}</select></td>
    <td class="muted small col-sm-hide">${a.messages || 0}</td>
    <td class="muted small col-sm-hide">${esc(a.last_ts || "—")}</td>
  </tr>`);
  body.innerHTML = rows.join("") || '<tr><td colspan="4" class="empty">还没有发言记录，可在上方直接填 openid 添加</td></tr>';
}
function renderPermTools() {
  const body = qs("#perm-tool-body");
  if (!body) return;
  const rows = (state.tools || []).map((t) => `<tr>
    <td>
      <div class="mono">${esc(t.name)}</div>
      ${t.enabled ? "" : '<div class="muted small">已停用</div>'}
      ${t.level_default != null && Number(t.min_level) !== Number(t.level_default)
        ? `<div class="muted small">默认 L${t.level_default}</div>` : ""}
    </td>
    <td class="muted small col-sm-hide">${esc(toolCat(t.category))}</td>
    <td><select class="sel small-input" data-tool-level data-tool="${esc(t.name)}">${levelOptions(t.min_level, 1, state.lockedLevel)}</select></td>
    <td class="muted small col-sm-hide">${esc(t.description)}</td>
  </tr>`);
  body.innerHTML = rows.join("") || '<tr><td colspan="4" class="empty">（无工具）</td></tr>';
}
async function addActor() {
  const openid = qs("#actor-openid").value.trim();
  const name = qs("#actor-name").value.trim();
  const level = Number(qs("#actor-level").value);
  if (!openid) { toast("请填写 openid", "error"); return; }
  try {
    await api("/api/actors", { method: "POST", body: { openid, level, name: name || null } });
    qs("#actor-openid").value = "";
    qs("#actor-name").value = "";
    toast(`已设为 ${levelLabel(level)}`, "success");
  } catch (err) { toast(err.message, "error"); }
  guarded("permissions", refreshPermissions);
}
async function saveActorLevel(openid, level) {
  try {
    await api("/api/actors", { method: "POST", body: { openid, level: Number(level) } });
    toast(`已把 ${openid.slice(0, 8)} 设为 ${levelLabel(level)}`, "success");
  } catch (err) { toast(err.message, "error"); }
  guarded("permissions", refreshPermissions);
}
async function saveToolLevel(name, level) {
  try {
    const d = await api(`/api/tools/${encodeURIComponent(name)}/level`, {
      method: "POST", body: { level: Number(level) },
    });
    toast(`已把 ${name} 门槛设为 ${levelLabel(d.level)}`, "success");
  } catch (err) { toast(err.message, "error"); }
  guarded("permissions", refreshPermissions);
}

/* ================= 日志 ================= */
async function refreshLogs() {
  const params = new URLSearchParams({ lines: "400", level: qs("#log-level").value });
  const sess = qs("#log-session").value.trim();
  const q = qs("#log-q").value.trim();
  if (sess) params.set("session", sess);
  if (q) params.set("q", q);
  const d = await api(`/api/logs?${params}`);
  if (d.file) qs("#log-file").textContent = d.file;
  const lvClass = { DEBUG: "lv-debug", INFO: "lv-info", WARNING: "lv-warning", ERROR: "lv-error", CRITICAL: "lv-critical" };
  const rows = (d.logs || []).map((l) => {
    const lv = lvClass[l.level] || "";
    return `<div class="log-line ${lv}">
      <span class="log-ts">${esc(fmtLogTs(l.ts))}</span>
      <span class="log-lv"><span class="lv ${lv}">${esc(l.level)}</span></span>
      <span class="log-sess">${logSessChip(l.session)}</span>
      <span class="log-msg">${esc(l.content)}</span>
    </div>`;
  }).join("");
  const wrap = qs(".log-wrap");
  const follow = qs("#log-auto").checked && (
    !state.logsRendered || !wrap || wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 48
  );
  qs("#log-body").innerHTML = rows || '<div class="empty">（无日志）</div>';
  state.logsRendered = true;
  if (follow && wrap) wrap.scrollTop = wrap.scrollHeight;
}

/* ================= 刷新 ================= */
async function refreshAll() {
  await Promise.allSettled([
    guarded("status", refreshStatus),
    guarded("stats", refreshStats),
    guarded("sessions", refreshSessions),
    guarded("recent", refreshDashRecent),
    guarded("messages", refreshMessages),
    guarded("memories", refreshMemories),
    guarded("config", loadConfig),
    guarded("permissions", refreshPermissions),
    guarded("tools", refreshTools),
    guarded("logs", refreshLogs),
  ]);
}

/* ================= 事件 ================= */
document.addEventListener("click", async (event) => {
  const pageBtn = event.target.closest("[data-page]");
  if (pageBtn) { switchPage(pageBtn.dataset.page); return; }

  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  const id = el.dataset.id;
  const sid = el.dataset.sid;

  switch (action) {
    case "toggle-sidebar":
      qs("#sidebar").classList.toggle("open");
      qs("#side-overlay").classList.toggle("hidden", !qs("#sidebar").classList.contains("open"));
      break;
    case "close-sidebar":
      closeSidebar();
      break;
    case "toggle-rail":
      setRail(!qs("#app").classList.contains("rail"));
      break;
    case "refresh":
      toast("正在刷新…", "info");
      refreshAll();
      break;
    case "logout":
      doLogout();
      break;
    case "refresh-sessions":
      guarded("sessions", refreshSessions);
      break;
    case "refresh-messages":
      guarded("messages", refreshMessages);
      break;
    case "refresh-logs":
      guarded("logs", refreshLogs);
      break;
    case "refresh-tools":
      guarded("tools", refreshTools);
      break;
    case "refresh-permissions":
      guarded("permissions", refreshPermissions);
      break;
    case "actor-add":
      addActor();
      break;
    case "tool-cat":
      state.toolCat = el.dataset.cat || "";
      renderToolCats();
      renderTools();
      break;
    case "tool-toggle": {
      const name = el.dataset.tool;
      const enabled = el.dataset.enabled === "1";
      try {
        await api(`/api/tools/${encodeURIComponent(name)}/toggle`, {
          method: "POST", body: { enabled: !enabled },
        });
        toast(`已${enabled ? "停用" : "启用"} ${name}`, "success");
        refreshTools();
      } catch (err) { toast(err.message, "error"); }
      break;
    }
    case "session-view":
      openSessionDetail(sid);
      break;
    case "session-clear":
      clearSession(sid);
      break;
    case "memory-search":
      searchMemories();
      break;
    case "memory-all":
      qs("#mem-q").value = "";
      state.memoryQuery = "";
      state.memoryUseSearch = false;
      guarded("memories", refreshMemories);
      break;
    case "memory-add":
      addMemory();
      break;
    case "memory-edit":
      openMemoryEdit(id);
      break;
    case "memory-save":
      saveMemory(id);
      break;
    case "memory-delete":
      deleteMemory(id);
      break;
    case "memory-tidy":
      tidyMemory();
      break;
    case "save-config":
      saveConfig();
      break;
    case "modal-close":
      closeModal();
      break;
    case "confirm-yes": {
      const r = state.confirmResolve;
      state.confirmResolve = null;
      closeModal();
      if (r) r(true);
      break;
    }
    case "confirm-no":
      closeModal(false);
      break;
    default:
      break;
  }
});

document.addEventListener("change", (event) => {
  const actorSel = event.target.closest("select[data-actor-level]");
  if (actorSel) {
    saveActorLevel(actorSel.dataset.openid, actorSel.value);
    return;
  }
  const toolSel = event.target.closest("select[data-tool-level]");
  if (toolSel) saveToolLevel(toolSel.dataset.tool, toolSel.value);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!qs("#modal-root").classList.contains("hidden")) closeModal(false);
    else closeSidebar();
  }
});

window.addEventListener("hashchange", () => {
  const name = location.hash.slice(1);
  if (PAGE_META[name] && name !== state.page) switchPage(name);
});

document.addEventListener("DOMContentLoaded", () => {
  qs("#login-form").addEventListener("submit", doLogin);

  let rail = false;
  try { rail = localStorage.getItem("ggbot.rail") === "1"; } catch (_) { /* ignore */ }
  setRail(rail);

  qs("#sess-q").addEventListener("input", renderSessions);
  qs("#msg-session").addEventListener("change", () => guarded("messages", refreshMessages));
  qs("#msg-role").addEventListener("change", () => guarded("messages", refreshMessages));
  qs("#msg-limit").addEventListener("change", () => guarded("messages", refreshMessages));
  qs("#mem-session").addEventListener("change", () => guarded("memories", refreshMemories));

  qs("#mem-q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchMemories(); });
  qs("#mem-add").addEventListener("keydown", (e) => { if (e.key === "Enter") addMemory(); });

  qs("#log-level").addEventListener("change", () => guarded("logs", refreshLogs));
  qs("#log-auto").addEventListener("change", (e) => {
    if (!e.target.checked) return;
    const wrap = qs(".log-wrap");
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
    guarded("logs", refreshLogs);
  });
  qsa("[data-path]").forEach((el) => {
    el.addEventListener("input", () => setConfigDirty(true));
    el.addEventListener("change", () => setConfigDirty(true));
  });
  let logTimer = null;
  ["#log-session", "#log-q"].forEach((sel) => {
    qs(sel).addEventListener("input", () => {
      clearTimeout(logTimer);
      logTimer = setTimeout(() => guarded("logs", refreshLogs), 350);
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !qs("#app").classList.contains("hidden")) guarded("status", refreshStatus);
  });

  boot();
});
