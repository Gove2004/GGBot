"use strict";

/* ================= API ================= */
async function api(path, options) {
  const opts = Object.assign({ credentials: "same-origin" }, options || {});
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    if (typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  }
  opts.headers = headers;
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (resp.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("登录已失效");
  }
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败（${resp.status}）`);
  }
  return data;
}

function guarded(name, fn) {
  if (state.loading[name]) return Promise.resolve();
  state.loading[name] = true;
  return Promise.resolve()
    .then(fn)
    .catch((err) => { if (err && err.message !== "登录已失效") toast(err.message, "error"); })
    .finally(() => { state.loading[name] = false; });
}

