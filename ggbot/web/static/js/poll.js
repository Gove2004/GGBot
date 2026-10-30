"use strict";

/* ================= 轮询 ================= */
function startPolling() {
  stopPolling();
  state.timers.push(setInterval(() => {
    if (!document.hidden) guarded("status", refreshStatus);
  }, 1000));
  state.timers.push(setInterval(() => {
    if (document.hidden) return;
    guarded("stats", refreshStats);
    guarded("sessions", refreshSessions);
    if (state.page === "messages") guarded("messages", refreshMessages);
    if (state.page === "dash") guarded("recent", refreshDashRecent);
    if (state.page === "reminders") guarded("reminders", refreshReminders);
  }, 1000));
  state.timers.push(setInterval(() => {
    if (document.hidden || state.page !== "logs") return;
    if (!qs("#log-auto").checked) return;
    guarded("logs", refreshLogs);
  }, 1000));
}

function stopPolling() {
  state.timers.forEach((t) => clearInterval(t));
  state.timers = [];
}

