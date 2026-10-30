"use strict";

/* ================= 工具 ================= */
const TOOL_CATS = { core: "控制", schedule: "定时", media: "富媒体", memory: "记忆", web: "联网", mail: "邮件", admin: "管理" };
const TOOL_CAT_ORDER = ["core", "schedule", "media", "memory", "web", "mail", "admin"];
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

