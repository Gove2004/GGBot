"use strict";

/* ================= 提示 / 弹窗 ================= */
let lastErrorToast = { msg: "", at: 0 };
function toast(msg, type) {
  if (type === "error") {
    const now = Date.now();
    if (lastErrorToast.msg === msg && now - lastErrorToast.at < 4000) return;
    lastErrorToast = { msg, at: now };
  }
  const stack = qs("#toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${type || "info"}`;
  el.innerHTML = `<span class="toast-ico">${type === "success" ? "✓" : type === "error" ? "!" : "i"}</span>
    <div class="toast-body">${esc(msg)}</div>
    <button class="toast-x" type="button" aria-label="关闭">×</button>
    <span class="toast-bar"></span>`;
  el.querySelector(".toast-x").addEventListener("click", () => el.remove());
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function openModal(title, body, foot) {
  qs("#modal-title").textContent = title;
  qs("#modal-body").innerHTML = body || "";
  qs("#modal-foot").innerHTML = foot || "";
  qs("#modal-root").classList.remove("hidden");
}

function closeModal(resolveValue) {
  qs("#modal-root").classList.add("hidden");
  const r = state.confirmResolve;
  state.confirmResolve = null;
  if (r && resolveValue !== undefined) r(resolveValue);
}

function confirmAction(title, text, okLabel) {
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
    openModal(
      title,
      `<p class="muted">${esc(text)}</p>`,
      `<button class="btn ghost" data-action="confirm-no">取消</button>
       <button class="btn danger" data-action="confirm-yes">${esc(okLabel || "确定")}</button>`
    );
  });
}

