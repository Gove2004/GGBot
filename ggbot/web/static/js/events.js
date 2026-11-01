"use strict";

/* ================= 事件 ================= */
document.addEventListener("click", async (event) => {
  const pageBtn = event.target.closest("[data-page]");
  if (pageBtn) { switchPage(pageBtn.dataset.page); return; }

  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  const id = el.dataset.id;
  const sid = el.dataset.sid;

  switch (action) {
    case "toggle-sidebar":
      qs("#sidebar").classList.toggle("open");
      qs("#side-overlay").classList.toggle("hidden", !qs("#sidebar").classList.contains("open"));
      break;
    case "close-sidebar":
      closeSidebar();
      break;
    case "toggle-rail":
      setRail(!qs("#app").classList.contains("rail"));
      break;
    case "refresh":
      toast("正在刷新…", "info");
      refreshAll();
      break;
    case "logout":
      doLogout();
      break;
    case "refresh-sessions":
      guarded("sessions", refreshSessions);
      break;
    case "refresh-emojis":
      // 手动刷新就是要立刻重扫目录（子目录里刚丢的图，扫目录缓存可能还没跟上）
      guarded("emojis", () => refreshEmojis(true));
      break;
    case "refresh-logs":
      guarded("logs", refreshLogs);
      break;
    case "refresh-schedule":
      guarded("schedule", refreshSchedule);
      break;
    case "refresh-tools":
      guarded("tools", refreshTools);
      break;
    case "refresh-permissions":
      guarded("permissions", refreshPermissions);
      break;
    case "actor-add":
      addActor();
      break;
    case "tool-cat":
      state.toolCat = el.dataset.cat || "";
      renderToolCats();
      renderTools();
      break;
    case "tool-toggle": {
      const name = el.dataset.tool;
      const enabled = el.dataset.enabled === "1";
      try {
        await api(`/api/tools/${encodeURIComponent(name)}/toggle`, {
          method: "POST", body: { enabled: !enabled },
        });
        toast(`已${enabled ? "停用" : "启用"} ${name}`, "success");
        refreshTools();
      } catch (err) { toast(err.message, "error"); }
      break;
    }
    case "session-view":
      openSessionDetail(sid);
      break;
    case "session-clear":
      clearSession(sid);
      break;
    case "memory-search":
      searchMemories();
      break;
    case "memory-all":
      qs("#mem-q").value = "";
      state.memoryQuery = "";
      state.memoryUseSearch = false;
      guarded("memories", refreshMemories);
      break;
    case "memory-add":
      addMemory();
      break;
    case "memory-edit":
      openMemoryEdit(id);
      break;
    case "memory-save":
      saveMemory(id);
      break;
    case "memory-delete":
      deleteMemory(id);
      break;
    case "memory-purge":
      purgeMemory(el.dataset.subject, el.dataset.kind, el.dataset.session);
      break;
    case "memory-sweep":
      sweepMemory();
      break;
    case "schedule-edit":
      openScheduleEdit(id);
      break;
    case "schedule-save":
      saveSchedule(id);
      break;
    case "schedule-cancel":
      cancelSchedule(id);
      break;
    case "schedule-delete":
      deleteSchedule(id);
      break;
    case "schedule-purge":
      openSchedulePurge();
      break;
    case "schedule-purge-run":
      purgeSchedule();
      break;
    case "schedule-sweep":
      sweepSchedule();
      break;
    case "schedule-more":
      loadMoreSchedule();
      break;
    case "emoji-view":
      openEmoji(el.dataset.file);
      break;
    case "emoji-pick":
      qs("#emoji-file").click();
      break;
    case "emoji-save-desc":
      saveEmojiDesc(el.dataset.file);
      break;
    case "emoji-delete":
      deleteEmoji(el.dataset.file);
      break;
    case "save-config":
      saveConfig();
      break;
    case "refresh-persona":
      guarded("persona", () => refreshPersona(true));
      break;
    case "persona-default":
      fillPersonaDefault();
      break;
    case "persona-save":
      savePersona();
      break;
    case "modal-close":
      closeModal();
      break;
    case "confirm-yes": {
      const r = state.confirmResolve;
      state.confirmResolve = null;
      closeModal();
      if (r) r(true);
      break;
    }
    case "confirm-no":
      closeModal(false);
      break;
    default:
      break;
  }
});

document.addEventListener("change", (event) => {
  const actorSel = event.target.closest("select[data-actor-level]");
  if (actorSel) {
    saveActorLevel(actorSel.dataset.openid, actorSel.value);
    return;
  }
  const toolSel = event.target.closest("select[data-tool-level]");
  if (toolSel) saveToolLevel(toolSel.dataset.tool, toolSel.value);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!qs("#modal-root").classList.contains("hidden")) closeModal(false);
    else closeSidebar();
  }
});

window.addEventListener("hashchange", () => {
  const name = location.hash.slice(1);
  if (PAGE_META[name] && name !== state.page) switchPage(name);
});

document.addEventListener("DOMContentLoaded", () => {
  qs("#login-form").addEventListener("submit", doLogin);

  let rail = false;
  try { rail = localStorage.getItem("ggbot.rail") === "1"; } catch (_) { /* ignore */ }
  setRail(rail);

  qs("#sess-q").addEventListener("input", renderSessions);
  qs("#emoji-q").addEventListener("input", renderEmojis);
  // 选完就上传，不排队：传完清空，同一张图可以连着试两次
  qs("#emoji-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (file) uploadEmoji(file);
  });
  qs("#persona-text").addEventListener("input", renderPersonaMeta);
  qs("#mem-session").addEventListener("change", () => guarded("memories", refreshMemories));
  qs("#mem-kind").addEventListener("change", () => guarded("memories", refreshMemories));
  // 换筛选条件时把载入窗口收回默认，否则会带着上一次「载入更多」的偏移量
  ["#sch-session", "#sch-status"].forEach((sel) =>
    qs(sel).addEventListener("change", () => {
      state.schLimit = 0;
      guarded("schedule", refreshSchedule);
    }));
  let schTimer = null;
  qs("#sch-q").addEventListener("input", () => {
    clearTimeout(schTimer);
    schTimer = setTimeout(() => {
      state.schLimit = 0;
      guarded("schedule", refreshSchedule);
    }, 350);
  });

  qs("#mem-q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchMemories(); });
  qs("#mem-add").addEventListener("keydown", (e) => { if (e.key === "Enter") addMemory(); });

  qs("#log-level").addEventListener("change", () => guarded("logs", refreshLogs));
  qs("#log-auto").addEventListener("change", (e) => {
    if (!e.target.checked) return;
    const wrap = qs(".log-wrap");
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
    guarded("logs", refreshLogs);
  });
  qsa("[data-path]").forEach((el) => {
    el.addEventListener("input", () => setConfigDirty(true));
    el.addEventListener("change", () => setConfigDirty(true));
  });
  let logTimer = null;
  ["#log-session", "#log-q"].forEach((sel) => {
    qs(sel).addEventListener("input", () => {
      clearTimeout(logTimer);
      logTimer = setTimeout(() => guarded("logs", refreshLogs), 350);
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !qs("#app").classList.contains("hidden")) guarded("status", refreshStatus);
  });

  boot();
});
