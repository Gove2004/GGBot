"use strict";

/* ================= 页面切换 ================= */
function switchPage(name) {
  if (!PAGE_META[name]) name = "dash";
  state.page = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  qsa(".page").forEach((p) => p.classList.add("hidden"));
  const page = qs(`#page-${name}`);
  if (page) page.classList.remove("hidden");
  qsa(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === name));
  const meta = PAGE_META[name];
  qs("#page-title").textContent = meta[0];
  qs("#page-sub").textContent = meta[1];
  closeSidebar();
  if (name === "dash") {
    guarded("status", refreshStatus);
    guarded("stats", refreshStats);
    guarded("sessions", refreshSessions);
    guarded("recent", refreshDashRecent);
  } else if (name === "sessions") {
    guarded("sessions", refreshSessions);
  } else if (name === "messages") {
    guarded("sessions", refreshSessions);
    guarded("messages", refreshMessages);
  } else if (name === "memory") {
    guarded("sessions", refreshSessions);
    guarded("memories", refreshMemories);
  } else if (name === "settings") {
    guarded("config", loadConfig);
  } else if (name === "permissions") {
    guarded("permissions", refreshPermissions);
  } else if (name === "tools") {
    guarded("tools", refreshTools);
  } else if (name === "logs") {
    state.logsRendered = false;
    guarded("logs", refreshLogs);
  } else if (name === "reminders") {
    guarded("sessions", refreshSessions);
    guarded("reminders", refreshReminders);
  }
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

