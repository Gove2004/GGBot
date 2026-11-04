"use strict";

/* ================= 轮询 ================= */
/* 两条定时器：外壳每秒一次（倒计时要走着看），当前页按 PAGE_LIVE 自己的节奏。
   不在 PAGE_LIVE 里的页面一个请求都不发——以前不管停在哪一页，每秒都要打 status / stats / sessions 三卡。 */

let pageTimer = null;

function startPolling() {
  stopPolling();
  state.timers.push(setInterval(() => {
    if (!document.hidden) refreshShell();
  }, 1000));
  armPageTimer();
}

/* 切页时重新挂：只有会自己变的那几页有定时器，间隔写在 PAGE_LIVE 里 */
function armPageTimer() {
  if (pageTimer) clearInterval(pageTimer);
  pageTimer = null;
  const every = PAGE_LIVE[state.page];
  if (!every) return;
  pageTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.page === "logs" && !qs("#log-auto").checked) return;
    reloadPage(state.page);
  }, every);
}

function stopPolling() {
  state.timers.forEach((t) => clearInterval(t));
  state.timers = [];
  if (pageTimer) clearInterval(pageTimer);
  pageTimer = null;
}
