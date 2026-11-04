"use strict";

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

function markRestartFields(paths) {
  qsa(".field .restart-hint").forEach((el) => el.remove());
  new Set(paths || []).forEach((path) => {
    const label = qs(`[data-path="${path}"]`)?.closest(".field")?.querySelector("span");
    if (!label) return;
    const hint = document.createElement("i");
    hint.className = "badge warn restart-hint";
    hint.textContent = "需重启";
    label.appendChild(hint);
  });
}

function fillConfigFields(config) {
  qsa("[data-path]").forEach((el) => {
    const v = getPath(config, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else if (v !== undefined && v !== null) el.value = v;
  });
}

/* 设置页一次拿到 {config,allowed,restart}：config 是脱敏后的整份配置，
   restart 是后端点名的「改这几项得重启」，前端只负责把标记挂到对应字段上。 */
function renderSettings(d) {
  fillConfigFields(d.config);
  markRestartFields(d.restart);
  setConfigDirty(false);
  qs("#cfg-msg").textContent = "修改后点击保存，写回 config.json 并热生效（标了「需重启」的除外）";
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
      // firstChild：徽标（需重启）是后加的节点，别把它拼进提示文案
      bad = el.closest(".field")?.querySelector("span")?.firstChild?.textContent?.trim() || el.dataset.path;
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
    if (r.config) fillConfigFields(r.config);
  } catch (err) {
    toast(err.message, "error");
    setConfigDirty(true);
  }
}

