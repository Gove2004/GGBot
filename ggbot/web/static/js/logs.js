"use strict";

/* ================= 日志 ================= */
async function refreshLogs() {
  const params = new URLSearchParams({ lines: "400", level: qs("#log-level").value });
  const sess = qs("#log-session").value.trim();
  const q = qs("#log-q").value.trim();
  if (sess) params.set("session", sess);
  if (q) params.set("q", q);
  const d = await api(`/api/logs?${params}`);
  if (d.file) qs("#log-file").textContent = d.file;
  // 行的类名是 log-<级别>，徽章才是 lv-<级别>：两者同名时徽章的半透明底色会涂满整行
  const badgeCls = { DEBUG: "lv-debug", INFO: "lv-info", WARNING: "lv-warning", ERROR: "lv-error", CRITICAL: "lv-critical" };
  const rows = (d.logs || []).map((l) => {
    const key = String(l.level || "").toLowerCase();
    const lv = badgeCls[l.level] || "";
    return `<div class="log-line${key ? ` log-${key}` : ""}">
      <span class="log-ts">${esc(fmtLogTs(l.ts))}</span>
      <span class="log-lv"><span class="lv ${lv}">${esc(l.level)}</span></span>
      <span class="log-sess">${logSessChip(l.session)}</span>
      <span class="log-msg">${esc(l.content)}</span>
    </div>`;
  }).join("");
  const wrap = qs(".log-wrap");
  const follow = qs("#log-auto").checked && (
    !state.logsRendered || !wrap || wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 48
  );
  qs("#log-body").innerHTML = rows || `<div class="empty">${
    sess || q ? "筛选后没有匹配（换个关键词，或清掉筛选条件）" : "（无日志）"
  }</div>`;
  const shown = (d.logs || []).length;
  const hit = d.total != null ? d.total : shown;
  qs("#log-count").textContent = hit > shown
    ? ` · 命中 ${hit} 条，显示最近 ${shown} 条`
    : ` · 显示最近 ${shown} 条`;
  state.logsRendered = true;
  if (follow && wrap) wrap.scrollTop = wrap.scrollHeight;
}

