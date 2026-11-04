"use strict";

/* ================= 记忆 ================= */
const KIND_LABEL = { profile: "档案", event: "事件" };

/* 记忆页一次拿到 {items,total,limits,usage,searched}。
   searched 是后端说的「这批按相关度检索」，前端据此换分组方式，不再自己猜；
   检索时 kind 只能在这批命中里再筛——向量检索本身不认类别。 */
function renderMemories(d) {
  const kind = qs("#mem-kind").value;
  state.memLimits = d.limits || {};
  state.memUsage = d.usage || null;
  const items = d.items || [];
  paintMemories(kind && d.searched ? items.filter((m) => m.kind === kind) : items, d.total, d.searched);
}

/* 临界提示只在记忆页出现：整库和「某个人」两条上限都会先撞。
   读数用的是遗忘扫描的口径（countable 已经扣掉到期与淡出），
   所以这里说的「快到顶」就是扫描之前真的快到顶，不会是个假警报。 */
function memCap() {
  const el = qs("#mem-cap");
  const u = state.memUsage;
  const hide = () => { el.className = "mem-cap hidden"; el.innerHTML = ""; };
  if (!u || !u.max_memories) return hide();
  const used = Number(u.countable || 0);
  const cap = Number(u.max_memories || 0);
  const topCount = Number(u.top_count || 0);
  const topCap = Number(u.max_per_subject || 0);
  const rate = cap ? used / cap : 0;
  const subjOver = !!(u.top_subject && topCount > topCap);
  const over = rate >= 1 || subjOver;
  if (!over && rate < 0.8) return hide();
  const parts = [`上限用量 <strong>${used} / ${cap}</strong>（${Math.round(rate * 100)}%）`];
  const resting = Number(u.expired || 0) + Number(u.faded || 0);
  if (resting) parts.push(`另有 ${resting} 条到期或淡出，扫描时先腾掉`);
  if (u.top_subject) {
    parts.push(subjOver
      ? `关于「${esc(u.top_subject)}」的档案 <strong>${topCount} / ${topCap}</strong>，已超每人上限`
      : `最满的是「${esc(u.top_subject)}」${topCount} / ${topCap}`);
  }
  if (over) parts.push("再写就要按保留分腾最弱的那批");
  el.className = "mem-cap " + (over ? "over" : "warn");
  el.innerHTML = `<span>${(over ? "记忆已到上限 · " : "记忆接近上限 · ") + parts.join(" · ")}</span>
    <button class="btn ghost small" data-action="memory-sweep">立即遗忘扫描</button>`;
}

function memHead(total, isSearch) {
  const l = state.memLimits || {};
  const base = isSearch
    ? `检索 “${esc(state.memoryQuery)}” · ${state.memItems.length} 条结果`
    : `长期记忆 · 共 ${total == null ? state.memItems.length : total} 条`;
  const limits = l.max_memories
    ? ` · 整库上限 ${l.max_memories} · 每人档案 ${l.max_per_subject} · 事件 ${l.forget_after_days} 天不用就淡出`
    : "";
  qs("#mem-total").innerHTML = base + limits;
}

function memRow(m) {
  const imp = Math.max(1, Math.min(5, Number(m.importance) || 1));
  const kind = m.kind === "event" ? "event" : "profile";
  const ret = Number(m.retention || 0).toFixed(2);
  const rel = m.score != null ? `<div class="score">相关 ${Number(m.score).toFixed(3)}</div>` : "";
  const scope = m.session_id ? sessChip(m.session_id, "tiny") : '<span class="badge gray">全局</span>';
  const expire = m.expires_at
    ? `${esc(String(m.expires_at).slice(0, 10))}${m.expired ? ' <span class="badge fail">已过</span>' : ""}`
    : '<span class="muted small">不限</span>';
  return `<tr>
    <td><span class="stars">${"★".repeat(imp)}</span><div class="score">${ret}</div>${rel}</td>
    <td>${esc(m.content)}</td>
    <td class="col-sm-hide"><span class="badge${kind === "event" ? " gray" : ""}">${KIND_LABEL[kind]}</span> ${m.subject ? esc(m.subject) : '<span class="muted small">未归属</span>'} ${scope}</td>
    <td class="muted small col-sm-hide">${Number(m.hit_count) || 0} 次 · 安静 ${Number(m.quiet_days) || 0} 天</td>
    <td class="muted small col-sm-hide">${expire}</td>
    <td class="col-actions"><div class="row-actions">
      <button class="btn ghost small" data-action="memory-edit" data-id="${esc(m.id)}">编辑</button>
      <button class="btn danger small" data-action="memory-delete" data-id="${esc(m.id)}">删除</button>
    </div></td>
  </tr>`;
}

// 组尾一行：整组清理的入口，只在这一组里有条目时才出现
function groupFoot(label, count, subject, kind) {
  const scope = qs("#mem-session").value;
  return `<tr class="mem-group">
    <td colspan="6">
      <span class="muted small">${esc(label)} · ${count} 条</span>
      <button class="btn ghost small" data-action="memory-purge"
        data-subject="${esc(subject || "")}" data-kind="${esc(kind)}" data-session="${esc(scope)}">清掉这一组</button>
    </td>
  </tr>`;
}

function paintMemories(items, total, isSearch) {
  state.memItems = items || [];
  memHead(total, isSearch);
  memCap();
  const body = qs("#mem-body");
  if (!state.memItems.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">暂无记忆</td></tr>';
    return;
  }
  const profiles = state.memItems.filter((m) => (m.kind || "profile") === "profile");
  const events = state.memItems.filter((m) => m.kind === "event");
  const html = [];
  if (isSearch) {
    // 检索结果是按相关度排的，按人分组会把分数顺序打乱
    html.push(profiles.map(memRow).join(""), events.map(memRow).join(""));
  } else {
    const bySubject = new Map();
    for (const m of profiles) {
      const key = m.subject || "";
      if (!bySubject.has(key)) bySubject.set(key, []);
      bySubject.get(key).push(m);
    }
    const named = [...bySubject.keys()].filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-CN"));
    for (const subject of named) {
      const rows = bySubject.get(subject);
      html.push(rows.map(memRow).join(""), groupFoot(`关于 ${subject}`, rows.length, subject, "profile"));
    }
    const loose = bySubject.get("") || [];
    if (loose.length) html.push(loose.map(memRow).join(""), groupFoot("未归属的档案", loose.length, "", "profile"));
    if (events.length) html.push(events.map(memRow).join(""), groupFoot("事件流水", events.length, "", "event"));
  }
  body.innerHTML = html.join("");
}

async function addMemory() {
  const input = qs("#mem-add");
  const content = input.value.trim();
  if (!content) return;
  const body = {
    content,
    kind: qs("#mem-add-kind").value,
    subject: qs("#mem-add-subject").value.trim(),
    importance: Number(qs("#mem-importance").value),
    session_id: qs("#mem-session").value || null,
  };
  const expires = qs("#mem-add-expires").value;
  if (expires) body.expires_at = expires;
  try {
    const r = await api("/api/memories", { method: "POST", body });
    input.value = "";
    qs("#mem-add-subject").value = "";
    qs("#mem-add-expires").value = "";
    const note = r.updated
      ? `已改写 ${r.updated} 条旧记忆`
      : r.dropped
        ? "已有同样的记忆，未重复写入"
        : "已写入";
    toast(`记忆${note}`, "success");
    reloadPage();
    refreshShell();
  } catch (err) { toast(err.message, "error"); }
}

async function searchMemories() {
  state.memoryQuery = qs("#mem-q").value.trim();
  state.memoryUseSearch = !!state.memoryQuery;
  await reloadPage("memory");
}

function openMemoryEdit(id) {
  const item = state.memItems.find((x) => x.id === id);
  if (!item) return;
  const kind = item.kind === "event" ? "event" : "profile";
  openModal(
    "编辑记忆",
    `<label class="field"><span>内容</span><textarea id="mem-edit-content">${esc(item.content)}</textarea></label>
     <label class="field"><span>类别</span><select id="mem-edit-kind" class="sel">
       <option value="profile">人物档案（关于一个人的长期事实，会互相取代）</option>
       <option value="event">事件流水（发生过的事，很久不用会淡出）</option>
     </select></label>
     <label class="field"><span>关于谁</span><input id="mem-edit-subject" class="input" value="${esc(item.subject || "")}" placeholder="留空表示不归属具体的人"></label>
     <label class="field"><span>重要度</span><select id="mem-edit-importance" class="sel">
       <option value="1">★</option><option value="2">★★</option><option value="3">★★★</option>
       <option value="4">★★★★</option><option value="5">★★★★★</option>
     </select></label>
     <label class="field"><span>到期日</span><input id="mem-edit-expires" class="input" type="date" value="${esc((item.expires_at || "").slice(0, 10))}"><span class="muted small">留空表示不过期</span></label>`,
    `<button class="btn ghost" data-action="modal-close">取消</button>
     <button class="btn primary" data-action="memory-save" data-id="${esc(id)}">保存</button>`
  );
  qs("#mem-edit-kind").value = kind;
  qs("#mem-edit-importance").value = String(Math.max(1, Math.min(5, Number(item.importance) || 1)));
}

async function saveMemory(id) {
  const content = qs("#mem-edit-content").value.trim();
  if (!content) { toast("内容不能为空", "error"); return; }
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: {
        content,
        kind: qs("#mem-edit-kind").value,
        subject: qs("#mem-edit-subject").value.trim(),
        importance: Number(qs("#mem-edit-importance").value),
        expires_at: qs("#mem-edit-expires").value || "",
      },
    });
    closeModal();
    toast("记忆已更新", "success");
    reloadPage();
  } catch (err) { toast(err.message, "error"); }
}

async function deleteMemory(id) {
  const ok = await confirmAction("删除记忆", "删除后无法恢复，确定继续？", "删除");
  if (!ok) return;
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("记忆已删除", "success");
    reloadPage();
    refreshShell();
  } catch (err) { toast(err.message, "error"); }
}

async function purgeMemory(subject, kind, session) {
  const who = subject ? `关于「${subject}」的档案` : (KIND_LABEL[kind] || "这一类");
  const params = new URLSearchParams();
  if (subject) params.set("subject", subject);
  if (kind) params.set("kind", kind);
  if (session) params.set("session_id", session);
  const ok = await confirmAction("清掉这一组",
    `删除${session ? labelFromKey(session) : "全部作用域"}里${who}的全部记忆，删除后无法恢复，确定继续？`, "删除");
  if (!ok) return;
  try {
    const r = await api(`/api/memories?${params}`, { method: "DELETE" });
    toast(`已删除 ${r.removed || 0} 条`, "success");
    reloadPage();
    refreshShell();
  } catch (err) { toast(err.message, "error"); }
}

async function sweepMemory() {
  const ok = await confirmAction("立即遗忘扫描",
    "依次清掉：已到期的、很久没被想起的事件、超出上限的档案与整库最弱的条目，并合并近似重复的记忆。",
    "开始扫描");
  if (!ok) return;
  try {
    const r = await api("/api/memories/sweep", { method: "POST" });
    toast(`到期 ${r.expired || 0} · 淡出 ${r.faded || 0} · 超限 ${r.over_cap || 0} · 合并 ${r.merged || 0}（剩 ${r.total || 0} 条）`, "success");
    reloadPage();
    refreshShell();
  } catch (err) { toast(err.message, "error"); }
}
