"use strict";

/* ================= 人设（系统提示词） ================= */
let personaBaseline = ""; // 最近一次从后端读到的生效文本，用来判断有没有未保存的改动
let personaDefault = "";  // 代码里的内置默认，「填入内置默认」用这份

function personaBox() {
  return qs("#persona-text");
}

function personaDirty() {
  return personaBox().value !== personaBaseline;
}

function renderPersonaMeta() {
  const text = personaBox().value;
  const max = state.personaMax || 20000;
  const lines = text ? text.split("\n").length : 0;
  const bits = [`字数 ${text.length} / ${max}`, `行 ${lines}`];
  if (personaDirty()) bits.push("有未保存的修改");
  const el = qs("#persona-meta");
  el.textContent = text.trim()
    ? bits.join(" · ") + " · 保存后下一轮生效（这段是缓存前缀，改一次会让前缀失效一次）"
    : "留空保存 = 回到代码里的内置默认人设";
  el.classList.toggle("warn", text.length > max);
}

function applyPersona(d) {
  personaDefault = d.default || "";
  personaBaseline = d.effective || "";
  state.personaMax = d.max_chars || 20000;
  personaBox().value = personaBaseline;
  const scope = qs("#persona-scope");
  scope.textContent = d.is_custom ? "自定义" : "内置默认";
  scope.classList.toggle("gray", !d.is_custom);
  scope.classList.toggle("ok", !!d.is_custom);
  renderPersonaMeta();
}

// 正在编辑时不重载，免得把没保存的改动冲掉；「重载」按钮传 force
async function refreshPersona(force) {
  if (!force && personaBaseline && personaDirty()) return;
  applyPersona(await api("/api/system-prompt"));
}

async function savePersona() {
  const text = personaBox().value;
  if (text.length > (state.personaMax || 20000)) {
    toast(`系统提示词最长 ${state.personaMax} 字，现在 ${text.length} 字`, "error");
    return;
  }
  try {
    const d = await api("/api/system-prompt", { method: "POST", body: { prompt: text } });
    applyPersona(d);
    toast(d.is_custom ? "人设已保存，下一轮生效" : "已清空，回到内置默认", "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

function fillPersonaDefault() {
  personaBox().value = personaDefault;
  renderPersonaMeta();
}
