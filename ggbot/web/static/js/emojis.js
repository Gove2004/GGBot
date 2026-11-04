"use strict";

/* ================= 表情包 ================= */
/* 表情页一次拿到 {items,mode,max_stickers,max_image_bytes}。
   force=1 只来自「刷新」按钮：刚丢进目录的图要立刻重扫一遍，平时吃服务端的扫目录缓存。 */
function renderEmojis(d) {
  state.emojiItems = d.items || [];
  state.emojiMode = d.mode || "none";
  state.emojiMax = Number(d.max_stickers) || 0;
  state.emojiMaxBytes = Number(d.max_image_bytes) || 0;
  paintEmojiGrid();
}

function filteredEmojis() {
  const q = (qs("#emoji-q").value || "").trim().toLowerCase();
  return state.emojiItems.filter((it) =>
    !q || `${it.desc || ""} ${it.file || ""}`.toLowerCase().includes(q)
  );
}

function paintEmojiGrid() {
  const body = qs("#emoji-body");
  if (!body) return;
  const rows = filteredEmojis();
  const total = state.emojiItems.length;
  const noVec = state.emojiMode === "vector"
    ? state.emojiItems.filter((it) => !it.has_vec).length : 0;
  let head = total
    ? `${total} 张素材 · 当前显示 ${rows.length}` + (state.emojiMax ? ` · 上限 ${state.emojiMax}` : "")
    : "素材库为空";
  if (total && state.emojiMode === "keyword") head += " · 没配 embedding，按字面匹配";
  if (noVec) head += ` · ${noVec} 张还没算向量`;
  qs("#emoji-total").textContent = head;
  body.innerHTML = rows.map((it) => `<button type="button" class="emoji-item" data-action="emoji-view" data-file="${esc(it.file)}" title="${esc(it.desc || it.file)}">
      <span class="emoji-thumb"><img loading="lazy" src="${esc(it.url)}" alt="${esc(it.desc || it.file)}"></span>
      <span class="emoji-desc">${esc(it.desc || "（无描述）")}</span>
      <span class="emoji-usage">${it.use_count ? `发过 ${it.use_count} 次` : "还没发过"}</span>
    </button>`).join("") || '<div class="empty">' + (total ? "没有匹配的素材" : "把图片放进 tool/media/emoji 目录，或点上面的「上传图」") + "</div>";
}

function emojiUrl(file) {
  return file.split("/").map(encodeURIComponent).join("/");
}

function openEmoji(file) {
  const it = state.emojiItems.find((x) => x.file === file);
  if (!it) return;
  const used = it.use_count
    ? `发过 ${it.use_count} 次 · 上次 ${esc(fmtTs(it.last_used_at))}`
    : "还没发过";
  const flag = (!it.has_vec && state.emojiMode === "vector")
    ? '<span class="badge warn">还没有向量</span>' : "";
  openModal(
    it.desc || it.file,
    `<div class="emoji-view"><img src="${esc(it.url)}" alt="${esc(it.desc || it.file)}"></div>
     <div class="emoji-meta">
       <span class="mono muted">${esc(it.file)}</span>
       <span class="muted">${esc(fmtTs(it.added_at))} 入库</span>
       <span class="muted">${used}</span>
       ${flag}
     </div>
     <label class="field"><span>描述（模型靠这一句挑图）</span>
       <textarea id="emoji-desc" rows="3" maxlength="200">${esc(it.desc)}</textarea></label>
     <p class="muted small">留空就退回文件名；改完立刻按新描述重算这一条的向量。「还没有向量」是刚丢进目录的图——发一次表情或下一次有人用 emoji_list 就补上。</p>`,
    `<button class="btn danger" data-action="emoji-delete" data-file="${esc(it.file)}">删除图片</button>
     <button class="btn ghost" data-action="modal-close">关闭</button>
     <button class="btn primary" data-action="emoji-save-desc" data-file="${esc(it.file)}">保存描述</button>`
  );
}

async function saveEmojiDesc(file) {
  const box = qs("#emoji-desc");
  if (!box) return;
  try {
    const out = await api(`/emoji/${emojiUrl(file)}`, {
      method: "PUT", body: { desc: box.value.trim() },
    });
    closeModal();
    toast(out.note || "已保存", "success");
    reloadPage("emojis", { force: "1" });
  } catch (err) { toast(err.message, "error"); }
}

async function deleteEmoji(file) {
  const ok = await confirmAction("删除素材",
    `会连 ${file} 这个图片文件一起删掉（目录是唯一真相，只删记录下次扫目录它又回来），删了就找不回了。`,
    "删除");
  if (!ok) return;
  try {
    await api(`/emoji/${emojiUrl(file)}`, { method: "DELETE" });
    toast("已删除", "success");
    reloadPage("emojis", { force: "1" });
  } catch (err) { toast(err.message, "error"); }
}

async function uploadEmoji(file) {
  if (state.emojiMaxBytes && file.size > state.emojiMaxBytes) {
    toast(`单张图片不超过 ${Math.round(state.emojiMaxBytes / 1024 / 1024)}MB`, "error");
    return;
  }
  try {
    // 原始字节直接进 body（没有 multipart 依赖），文件名与描述走查询参数
    const params = new URLSearchParams({ name: file.name });
    const out = await api(`/api/emojis/import?${params}`, { method: "POST", body: file });
    toast(out.note ? `${out.name} ${out.note}` : `已导入 ${out.name}`, "success");
    reloadPage("emojis", { force: "1" });
  } catch (err) { toast(err.message, "error"); }
}
