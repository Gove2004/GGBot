"use strict";

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

