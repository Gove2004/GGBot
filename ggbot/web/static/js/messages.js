"use strict";

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

