"use strict";

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

