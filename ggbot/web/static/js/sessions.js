"use strict";

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
    return [s.label, s.id, s.last_message].some((v) => String(v || "").toLowerCase().includes(q));
  });
  const top = list.scrollTop;
  list.innerHTML = rows.map((s) => {
    const l = s.live || { state: "offline", reason: "idle", remaining_sec: 0, idle_sec: null };
    const preview = s.last_message || "—";
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
  [["#mem-session", "全部作用域"], ["#rem-session", "全部会话"]].forEach(([sel, label]) => {
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
    const chats = (d.messages || []).map((m) => {
      const role = m.role === "assistant" ? "assistant" : "user";
      const pending = m.status === "pending" ? " · 待处理" : "";
      return `<div class="chat ${role}"><div class="chat-meta">${senderCell(m)} · ${esc(fmtTs(m.ts))}${pending}</div>${esc(m.content || "（无文本）")}</div>`;
    }).join("");
    const liveS = ((state.status && state.status.sessions) || []).find((x) => x.sid === sid);
    const stateCell = liveS
      ? `${stateBadge(liveS.state)}<span class="cell-note">${esc(reasonLabel(liveS.reason))} · 剩 ${Math.max(0, Math.round(liveS.remaining_sec || 0))}s</span>`
      : `${stateBadge("offline")}<span class="cell-note">静默</span>`;
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
        <span class="stat-chip">最近发言 <b>${esc(fmtTs(s.last_ts || s.updated_at))}</b></span>
        <span class="stat-chip sess-state">作息 ${stateCell}</span>
      </div>
      <div class="detail-scroll">
        <div class="modal-section"><h3>最近消息</h3><div class="chat-list">${chats || '<div class="empty">暂无消息</div>'}</div></div>
      </div>
    </div>`;
  } catch (err) {
    if (state.selectedSession !== sid) return;
    pane.innerHTML = `<div class="detail-empty"><p>${esc(err.message)}</p></div>`;
  }
}

async function clearSession(sid) {
  const ok = await confirmAction("清空上下文", `将删除该会话的全部消息，长期记忆不受影响。`, "清空");
  if (!ok) return;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(sid)}/context`, { method: "DELETE" });
    toast(`已清空 ${r.messages || 0} 条消息`, "success");
    await refreshSessions();
    openSessionDetail(sid);
    refreshStatus();
  } catch (err) { toast(err.message, "error"); }
}

