"use strict";

/* ================= 主题 =================
 * 颜色本身全在 base.css：暗色是 :root 的默认值，浅色只有 [data-theme="light"] 一处反转。
 * 这个文件只负责把 <html> 上的 data-theme 钉对。
 *
 * 它挂在 <head> 的样式表之后、正文之前（外链脚本会被未加载完的样式表挡住，所以此时 CSSOM 已就绪）：
 * CSP 是 script-src 'self'，内联脚本进不来，想「首帧就是对的色」只能靠一个外链文件抢在 body 之前跑。
 *
 * 三档状态：没表过态 = 跟随系统；手动按过 = 以用户选的为准。
 * → 只有手动按过才写 localStorage；没按过时系统翻主题，面板跟着翻。
 */
const THEME_KEY = "ggbot.theme";

function storedTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch (_) {
    return null;   // 隐私模式下 localStorage 直接抛，这时就退回「跟随系统」
  }
}

function systemTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  // theme-color 是手机浏览器地址栏的底色：值从令牌现读，免得同一件事在 HTML 里再抄一份十六进制、
  // 以后改 --bg 时漏掉它——地址栏颜色和页面颜色对不上是最容易被发现的错。
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (bg) meta.setAttribute("content", bg);
  }
  const btn = document.getElementById("theme-btn");
  if (btn) btn.title = theme === "light" ? "切回深色" : "切到浅色";
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* 存不了也照样切，只是下次进来又跟随系统 */ }
  applyTheme(next);
  return next;
}

applyTheme(storedTheme() || systemTheme());

if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const onChange = (e) => { if (!storedTheme()) applyTheme(e.matches ? "light" : "dark"); };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
}
