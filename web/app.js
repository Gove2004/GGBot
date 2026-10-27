"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const Q = (u, o) => fetch(u + (TOKEN ? (u.includes("?") ? "&" : "?") + "token=" + TOKEN : ""), o).then(r => r.json());
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[c]);

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
    `<tr><td class="muted">${esc(m.skey)}</td><td>${esc(m.role)}</td><td>${esc((m.content || "").slice(0, 60))}</td><td class="muted">${esc(m.status)}</td></tr>`).join("");
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

async function loadConfig() {
  const d = await Q("/api/config");
  const c = d.config || {};
  const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
  set("s-temperature", (c.deepseek || {}).request?.temperature);
  set("s-top_p", (c.deepseek || {}).request?.top_p);
  set("s-max_tokens", (c.deepseek || {}).request?.max_tokens);
  set("s-thinking", (c.deepseek || {}).request?.thinking);
  set("s-online_duration_sec", (c.behavior || {}).presence?.online_duration_sec);
  set("s-offline_interval_sec", (c.behavior || {}).presence?.offline_interval_sec);
  set("s-debounce_sec", (c.behavior || {}).reply?.debounce_sec);
  set("s-max_scan_messages", (c.behavior || {}).backlog?.max_scan_messages);
  set("s-batch_size", (c.behavior || {}).backlog?.batch_size);
  set("s-log_level", (c.web || {}).log_level);
}
async function saveConfig() {
  const g = id => document.getElementById(id).value;
  const items = [
    { path: "deepseek.request.temperature", value: parseFloat(g("s-temperature")) },
    { path: "deepseek.request.top_p", value: parseFloat(g("s-top_p")) },
    { path: "deepseek.request.max_tokens", value: parseInt(g("s-max_tokens")) },
    { path: "deepseek.request.thinking", value: g("s-thinking") },
    { path: "behavior.presence.online_duration_sec", value: parseInt(g("s-online_duration_sec")) },
    { path: "behavior.presence.offline_interval_sec", value: parseInt(g("s-offline_interval_sec")) },
    { path: "behavior.reply.debounce_sec", value: parseInt(g("s-debounce_sec")) },
    { path: "behavior.backlog.max_scan_messages", value: parseInt(g("s-max_scan_messages")) },
    { path: "behavior.backlog.batch_size", value: parseInt(g("s-batch_size")) },
    { path: "web.log_level", value: g("s-log_level") },
  ];
  const r = await Q("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(items) });
  document.getElementById("cfg-msg").textContent = "已更新: " + (r.updated || []).join(", ");
  loadConfig();
}
async function loadLogs() {
  const level = document.getElementById("log-level").value;
  const d = await Q("/api/logs?lines=120&level=" + level);
  document.getElementById("log-view").textContent = (d.lines || []).join("\n") || "(空)";
}

setInterval(refreshStatus, 2000);
setInterval(refreshMessages, 5000);
setInterval(loadLogs, 5000);
refreshStatus(); refreshMessages(); refreshMemories(); refreshRules(); refreshPersona(); loadConfig(); loadLogs();
