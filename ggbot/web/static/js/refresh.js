"use strict";

/* ================= 刷新 ================= */
async function refreshAll() {
  await Promise.allSettled([
    guarded("status", refreshStatus),
    guarded("stats", refreshStats),
    guarded("sessions", refreshSessions),
    guarded("recent", refreshDashRecent),
    guarded("emojis", refreshEmojis),
    guarded("memories", refreshMemories),
    guarded("config", loadConfig),
    guarded("permissions", refreshPermissions),
    guarded("tools", refreshTools),
    guarded("logs", refreshLogs),
    guarded("reminders", refreshReminders),
  ]);
}

