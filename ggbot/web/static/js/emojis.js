"use strict";

/* ================= 表情包 ================= */
async function refreshEmojis() {
  const d = await api("/api/emojis");
  state.emojiItems = (d && d.items) || [];
  renderEmojis();
}

function filteredEmojis() {
  const q = (qs("#emoji-q").value || "").trim().toLowerCase();
  return state.emojiItems.filter((it) =>
    !q || `${it.desc || ""} ${it.file || ""}`.toLowerCase().includes(q)
  );
}

function renderEmojis() {
  const body = qs("#emoji-body");
  if (!body) return;
  const rows = filteredEmojis();
  const total = state.emojiItems.length;
  qs("#emoji-total").textContent = total
    ? `${total} 张素材 · 当前显示 ${rows.length}`
    : "素材库为空";
  body.innerHTML = rows.map((it) => `<button type="button" class="emoji-item" data-action="emoji-view" data-file="${esc(it.file)}" title="${esc(it.desc || it.file)}">
      <span class="emoji-thumb"><img loading="lazy" src="${esc(it.url)}" alt="${esc(it.desc || it.file)}"></span>
      <span class="emoji-desc">${esc(it.desc || "（无描述）")}</span>
    </button>`).join("") || '<div class="empty">' + (total ? "没有匹配的素材" : "把图片放进 tool/media/emoji 目录") + "</div>";
}

function openEmoji(file) {
  const it = state.emojiItems.find((x) => x.file === file);
  if (!it) return;
  openModal(
    it.desc || it.file,
    `<div class="emoji-view"><img src="${esc(it.url)}" alt="${esc(it.desc || it.file)}"></div>
     <div class="emoji-meta">
       <span class="mono muted">${esc(it.file)}</span>
       <span class="muted">${esc(fmtTs(it.added_at))}</span>
     </div>
     <p class="muted small">${esc(it.desc || "（没有描述：模型只能靠文件名猜这张图）")}</p>`,
    `<button class="btn ghost" data-action="modal-close">关闭</button>`
  );
}
