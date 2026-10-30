"use strict";

/* ================= 登录 ================= */
function showLogin() {
  stopPolling();
  closeModal(false);
  qs("#app").classList.add("hidden");
  qs("#login").classList.remove("hidden");
  const input = qs("#login-token");
  input.value = "";
  setTimeout(() => input.focus(), 50);
}

function showLoginError(msg) {
  const el = qs("#login-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function showApp() {
  qs("#login").classList.add("hidden");
  qs("#login-error").classList.add("hidden");
  qs("#app").classList.remove("hidden");
  const initial = location.hash.slice(1);
  if (PAGE_META[initial]) state.page = initial;
  startPolling();
  switchPage(state.page || "dash");
  refreshAll();
}

async function boot() {
  try {
    const a = await api("/api/auth");
    if (a.authenticated) showApp();
    else showLogin();
  } catch (_) {
    showLogin();
  }
}

async function doLogin(event) {
  event.preventDefault();
  const input = qs("#login-token");
  const token = input.value.trim();
  if (!token) return;
  qs("#login-error").classList.add("hidden");
  try {
    await api("/api/login", { method: "POST", body: { token } });
    input.value = "";
    showApp();
    toast("已登录", "success");
  } catch (err) {
    showLoginError(err.message);
  }
}

async function doLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  showLogin();
  toast("已退出登录", "info");
}

