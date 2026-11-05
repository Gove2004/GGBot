"use strict";

/* ================= 权限 ================= */
/* 级别名字表与上下限只有一份来源：后端 _level_contract。权限页和工具页都带这份契约，
   谁先打开谁把它落到 state 上——前端不留兜底名字，也不留兜底数字。 */
function applyLevelContract(env) {
  if (!env) return;
  state.levelNames = env.level_names || {};
  state.maxLevel = env.max_level;
  state.lockedLevel = env.locked_level;
  state.lockedName = env.locked_name || "";
}
function levelLabel(n) {
  const num = Number(n);
  if (num >= state.lockedLevel) return state.lockedName;
  return `L${num} ${state.levelNames[num] || ""}`.trim();
}
function levelOptions(selected, lo, hi) {
  const out = [];
  for (let i = lo; i <= hi; i++) {
    out.push(`<option value="${i}"${Number(selected) === i ? " selected" : ""}>${esc(levelLabel(i))}</option>`);
  }
  return out.join("");
}
/* 权限页一次拿到三份读数：成员等级表 + 共用的工具清单（门槛就在这张表里改）+ 等级契约。 */
function renderPermissions(perms, tools, levels) {
  state.actors = perms.members || [];
  applyLevelContract(levels);
  state.defaultLevel = perms.default_level;
  const sub = qs("#perm-sub");
  if (sub) {
    sub.textContent = `发送者等级决定它能触发哪些工具 · 未登记成员默认 ${levelLabel(state.defaultLevel)}`;
  }
  const addSel = qs("#actor-level");
  if (addSel) addSel.innerHTML = levelOptions(state.defaultLevel, 0, state.maxLevel);
  setTools(tools);
  renderActors();
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
    <td class="muted small col-sm-hide col-ts">${esc(a.last_ts || "—")}</td>
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
    <td class="muted small col-sm-hide col-cat">${esc(toolCat(t.category))}</td>
    <td><select class="sel small-input" data-tool-level data-tool="${esc(t.name)}">${levelOptions(t.min_level, 1, state.lockedLevel)}</select></td>
    <td class="muted small col-sm-hide"><div class="cell-clamp" title="${esc(t.description)}">${esc(t.description)}</div></td>
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
  reloadPage("permissions");
}
async function saveActorLevel(openid, level) {
  try {
    await api("/api/actors", { method: "POST", body: { openid, level: Number(level) } });
    toast(`已把 ${openid.slice(0, 8)} 设为 ${levelLabel(level)}`, "success");
  } catch (err) { toast(err.message, "error"); }
  reloadPage("permissions");
}
async function saveToolLevel(name, level) {
  try {
    const d = await api(`/api/tools/${encodeURIComponent(name)}/level`, {
      method: "POST", body: { level: Number(level) },
    });
    toast(`已把 ${name} 门槛设为 ${levelLabel(d.level)}`, "success");
  } catch (err) { toast(err.message, "error"); }
  reloadPage("permissions");
}

