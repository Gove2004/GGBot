"use strict";

/* ================= 提醒 ================= */
const REMIND_STATUS = {
  pending: ["待触发", "warn"],
  fired: ["已触发", "ok"],
  cancelled: ["已取消", "gray"],
  expired: ["已过期", "fail"],
};

function remindBadge(status) {
  const m = REMIND_STATUS[status] || [status || "—", "gray"];
  return `<span class="badge ${m[1]}">${esc(m[0])}</span>`;
}
function remindSpan(sec) {
  const n = Math.abs(Math.floor(Number(sec) || 0));
  if (n < 60) return `${n}秒`;
  if (n < 3600) return `${Math.floor(n / 60)}分`;
  if (n < 86400) return `${Math.floor(n / 3600)}小时${Math.floor((n % 3600) / 60)}分`;
  return `${Math.floor(n / 86400)}天${Math.floor((n % 86400) / 3600)}小时`;
}
function remindDue(ts) {
  const d = new Date(Number(ts) * 1000);
  if (!isFinite(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function refreshReminders() {
  const params = new URLSearchParams({ status: qs("#rem-status").value });
  const sid = qs("#rem-session").value;
  if (sid) params.set("session_id", sid);
  const d = await api(`/api/reminders?${params}`);
  const now = Date.now() / 1000;
  // 待触发按到期时间从近到远排在前面，历史（已触发/已取消/已过期）按从新到旧
  const items = (d.items || []).slice().sort((a, b) => {
    const pa = a.status === "pending" ? 0 : 1;
    const pb = b.status === "pending" ? 0 : 1;
    if (pa !== pb) return pa - pb;
    return pa ? b.due_ts - a.due_ts : a.due_ts - b.due_ts;
  });
  qs("#rem-body").innerHTML = items.map((r) => {
    const gap = Number(r.due_ts) - now;
    const left = r.status === "pending"
      ? (gap >= 0 ? `${remindSpan(gap)}后` : `已过点 ${remindSpan(gap)}`)
      : `${remindSpan(gap)}${gap >= 0 ? "后" : "前"}`;
    const who = r.notify_name || r.owner_name || "本人";
    return `<tr>
      <td class="mono">#${esc(r.id)}</td>
      <td>${sessChip(r.session_id)}</td>
      <td class="col-sm-hide">${esc(who)}</td>
      <td>${esc(r.text)}</td>
      <td class="mono col-sm-hide">${esc(remindDue(r.due_ts))}</td>
      <td class="muted col-sm-hide">${esc(left)}</td>
      <td>${remindBadge(r.status)}</td>
      <td class="col-actions">${r.status === "pending"
        ? `<button class="btn ghost small" data-action="remind-cancel" data-id="${esc(r.id)}">取消</button>`
        : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="empty">${
    sid ? "这个会话没有符合条件的提醒" : "还没有提醒（对机器人说「五分钟后提醒我…」就会挂一条）"
  }</td></tr>`;
  const total = d.pending_total == null ? "—" : d.pending_total;
  qs("#rem-count").textContent = ` · 显示 ${items.length} 条 · 全局待触发 ${total} 条`;
}

async function cancelReminder(id) {
  const ok = await confirmAction("取消提醒", `取消 #${id} 后到点不会再发任何东西。`, "取消");
  if (!ok) return;
  try {
    await api(`/api/reminders/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    toast(`已取消 #${id}`, "success");
    refreshReminders();
  } catch (err) { toast(err.message, "error"); }
}
