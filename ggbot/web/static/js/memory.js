"use strict";

/* ================= 记忆 ================= */
async function refreshMemories() {
  const sid = qs("#mem-session").value;
  if (state.memoryUseSearch && state.memoryQuery) {
    const params = new URLSearchParams({ q: state.memoryQuery, k: "20" });
    if (sid) params.set("session_id", sid);
    const items = await api(`/api/memories/search?${params}`);
    renderMemories(items, null, true);
  } else {
    const params = new URLSearchParams({ limit: "100", offset: "0" });
    if (sid) params.set("session_id", sid);
    const d = await api(`/api/memories?${params}`);
    renderMemories(d.items || [], d.total, false);
  }
}

function renderMemories(items, total, isSearch) {
  state.memItems = items || [];
  qs("#mem-total").textContent = isSearch
    ? `检索 “${state.memoryQuery}” · ${state.memItems.length} 条结果`
    : `长期记忆 · 共 ${total == null ? state.memItems.length : total} 条`;
  const body = qs("#mem-body");
  body.innerHTML = state.memItems.map((m) => {
    const scope = m.session_id ? sessChip(m.session_id, "tiny") : '<span class="badge gray">全局</span>';
    const score = m.score != null ? `<div class="score">${Number(m.score).toFixed(3)}</div>` : "";
    return `<tr>
      <td><span class="stars">${"★".repeat(Math.max(1, Math.min(5, Number(m.importance) || 1)))}</span>${score}</td>
      <td>${esc(m.content)}</td>
      <td class="col-sm-hide">${scope}</td>
      <td class="muted small col-sm-hide">${esc(fmtTs(m.ts))}</td>
      <td class="col-actions"><div class="row-actions">
        <button class="btn ghost small" data-action="memory-edit" data-id="${esc(m.id)}">编辑</button>
        <button class="btn danger small" data-action="memory-delete" data-id="${esc(m.id)}">删</button>
      </div></td>
    </tr>`;
  }).join("");
  if (!state.memItems.length) body.innerHTML = '<tr><td colspan="5" class="empty">暂无记忆</td></tr>';
}

async function addMemory() {
  const input = qs("#mem-add");
  const content = input.value.trim();
  if (!content) return;
  const sid = qs("#mem-session").value || null;
  try {
    await api("/api/memories", {
      method: "POST",
      body: { content, importance: Number(qs("#mem-importance").value), session_id: sid },
    });
    input.value = "";
    toast("记忆已写入", "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

async function searchMemories() {
  state.memoryQuery = qs("#mem-q").value.trim();
  state.memoryUseSearch = !!state.memoryQuery;
  if (!state.memoryQuery) { refreshMemories(); return; }
  await guarded("memories", refreshMemories);
}

function openMemoryEdit(id) {
  const item = state.memItems.find((x) => x.id === id);
  if (!item) return;
  const imp = Math.max(1, Math.min(5, Number(item.importance) || 1));
  openModal(
    "编辑记忆",
    `<label class="field"><span>内容</span><textarea id="mem-edit-content">${esc(item.content)}</textarea></label>
     <label class="field"><span>重要度</span><select id="mem-edit-importance" class="sel">
       <option value="1">★</option><option value="2">★★</option><option value="3">★★★</option>
       <option value="4">★★★★</option><option value="5">★★★★★</option>
     </select></label>`,
    `<button class="btn ghost" data-action="modal-close">取消</button>
     <button class="btn primary" data-action="memory-save" data-id="${esc(id)}">保存</button>`
  );
  qs("#mem-edit-importance").value = String(imp);
}

async function saveMemory(id) {
  const content = qs("#mem-edit-content").value.trim();
  if (!content) { toast("内容不能为空", "error"); return; }
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: { content, importance: Number(qs("#mem-edit-importance").value) },
    });
    closeModal();
    toast("记忆已更新", "success");
    refreshMemories();
  } catch (err) { toast(err.message, "error"); }
}

async function deleteMemory(id) {
  const ok = await confirmAction("删除记忆", "删除后无法恢复，确定继续？", "删除");
  if (!ok) return;
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("记忆已删除", "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

async function tidyMemory() {
  const ok = await confirmAction("合并重复记忆",
    "按记忆条数自动放宽阈值（0.95 起，每多 50 条降 0.01，最低 0.86），合并近似重复条目，保留更重要的一条。",
    "开始整理");
  if (!ok) return;
  try {
    const r = await api("/api/memories/tidy", { method: "POST" });
    toast(`整理完成，合并删除 ${r.removed || 0} 条`, "success");
    refreshMemories();
    refreshStats();
  } catch (err) { toast(err.message, "error"); }
}

