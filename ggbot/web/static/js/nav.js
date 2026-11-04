"use strict";

/* ================= 页面切换 ================= */
function switchPage(name) {
  if (!PAGE_META[name]) name = "dash";
  state.page = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  qsa(".page").forEach((p) => p.classList.add("hidden"));
  const page = qs(`#page-${name}`);
  if (page) page.classList.remove("hidden");
  qsa(".nav-item").forEach((n) => {
    const on = n.dataset.page === name;
    n.classList.toggle("active", on);
    // 分组重排后组名靠 role=group 念出来，当前页要靠 aria-current，不然读屏只听得见一串同名按钮
    if (on) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  });
  const meta = PAGE_META[name];
  qs("#page-title").textContent = meta[0];
  qs("#page-sub").textContent = meta[1];
  closeSidebar();
  if (name === "logs") state.logsRendered = false;
  // 进页面只发一次请求：这一页要画的数据后端一次给齐（见 refresh.js）
  armPageTimer();
  reloadPage(name);
}

function closeSidebar() {
  qs("#sidebar").classList.remove("open");
  qs("#side-overlay").classList.add("hidden");
}

function setRail(on) {
  qs("#app").classList.toggle("rail", !!on);
  qsa(".nav-item").forEach((n) => {
    const t = n.querySelector("span");
    if (t) n.title = t.textContent;
  });
  try { localStorage.setItem("ggbot.rail", on ? "1" : "0"); } catch (_) { /* ignore */ }
}

