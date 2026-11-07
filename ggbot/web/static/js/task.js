"use strict";

/* ================= 定时任务 =================
 * 真相只在 tasks 表里（巡检循环读同一张表），所以这一页只做「看 + 改 + 取消 + 删 + 清理」，
 * 不给新建：排任务靠对话里的 when/repeat，面板再长一套时间输入就是第二个解析实现。
 */
const TASK_STATUS = {
  pending: ["在排", "warn"],
  fired: ["已跑完", "ok"],
  cancelled: ["已取消", "gray"],
  expired: ["已过期", "fail"],
  failed: ["已失败", "fail"],
};

function taskBadge(status) {
  const m = TASK_STATUS[status] || [status || "—", "gray"];
  return `<span class="badge ${m[1]}">${esc(m[0])}</span>`;
}

function taskSpan(sec) {
  const n = Math.abs(Math.floor(Number(sec) || 0));
  if (n < 60) return `${n}秒`;
  if (n < 3600) return `${Math.floor(n / 60)}分`;
  if (n < 86400) return `${Math.floor(n / 3600)}小时${Math.floor((n % 3600) / 60)}分`;
  return `${Math.floor(n / 86400)}天${Math.floor((n % 86400) / 3600)}小时`;
}

function taskDue(ts) {
  const d = new Date(Number(ts) * 1000);
  if (!isFinite(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function taskLocalInput(ts) {
  // datetime-local 要的是「本地墙上时间」那一格，写成 Z/带时区都不被接受
  const d = new Date(Number(ts) * 1000);
  if (!isFinite(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 待触发的分四堆：已经过点的、今天剩下的、这周之内的、更远的 */
function taskBucket(r, now) {
  if (r.status !== "pending") return "history";
  if (r.due_ts < now) return "overdue";
  const day = new Date(now * 1000);
  day.setHours(24, 0, 0, 0);
  if (r.due_ts < day.getTime() / 1000) return "today";
  if (r.due_ts < day.getTime() / 1000 + 6 * 86400) return "week";
  return "later";
}

const TASK_GROUP_LABEL = {
  overdue: ["已经过点", "fail"],
  today: ["今天要跑", "warn"],
  week: ["这周之内", "ok"],
  later: ["以后", "gray"],
  history: ["历史", "gray"],
};

function taskMeta(r) {
  const bits = [];
  if (r.rule) {
    const times = Number(r.times) || 0;
    const fired = Number(r.fired_count) || 0;
    bits.push(`<span class="badge on">${esc(r.rule_label)}${times > 0 ? ` ${fired}/${times}` : ` 已跑 ${fired}`}</span>`);
  }
  bits.push(sessChip(r.session_id, "tiny"));
  bits.push(`<span class="muted">汇报给 ${esc(r.notify_name || r.owner_name || "本人")}</span>`);
  bits.push(`<span class="muted mono">#${esc(r.id)}</span>`);
  if (Number(r.fail_count) > 0) {
    bits.push(`<span class="badge fail">失败 ${esc(r.fail_count)}${r.last_error ? ` · ${esc(r.last_error)}` : ""}</span>`);
  }
  if (r.status === "pending" && !r.rule) bits.push(taskBadge("pending"));
  if (r.status !== "pending") bits.push(taskBadge(r.status));
  if (r.fired_at) bits.push(`<span class="muted small">落定 ${esc(r.fired_at)}</span>`);
  return bits.join("");
}

function taskItem(r, now) {
  const gap = Number(r.due_ts) - now;
  const left = r.status === "pending"
    ? (gap >= 0 ? `${taskSpan(gap)}后` : `过点 ${taskSpan(gap)}`)
    : `${taskSpan(gap)}${gap >= 0 ? "后" : "前"}`;
  const ops = [];
  // 三个按钮都用两字标签：单字那颗会被 min-width 撑成正方形，一排看着高低不齐
  if (r.status === "pending") {
    ops.push(`<button class="btn ghost small" data-action="task-edit" data-id="${esc(r.id)}">改期</button>`);
    ops.push(`<button class="btn ghost small" data-action="task-cancel" data-id="${esc(r.id)}">取消</button>`);
  } else if (r.status === "failed") {
    ops.push(`<button class="btn ghost small" data-action="task-edit" data-id="${esc(r.id)}">重排</button>`);
  }
  ops.push(`<button class="btn ghost small" data-action="task-delete" data-id="${esc(r.id)}">删除</button>`);
  return `<div class="task-item ${taskBucket(r, now)}">
    <div class="task-when">
      <div class="task-clock mono">${esc(taskDue(r.due_ts))}</div>
      <div class="task-gap muted">${esc(left)}</div>
    </div>
    <div class="task-main">
      <div class="task-text">${esc(r.instruction)}</div>
      <div class="task-meta">${taskMeta(r)}</div>
    </div>
    <div class="task-ops">${ops.join("")}</div>
  </div>`;
}

const TASK_PAGE = 50;      // 一次拉 50 条；「载入更多」是把窗口撑大，不是翻页
const TASK_WINDOW = 500;   // 后端单次最多给 500 条，再要靠筛选缩小范围

/* 任务页一次拿到 {items,total,offset,stats,limits}；筛选参数由 refresh.js 从这几个控件读。 */
function renderTaskPage(d) {
  state.taskItems = d.items || [];
  state.taskTotal = Number(d.total) || 0;
  state.taskStats = d.stats || {};
  state.taskLimits = d.limits || {};
  paintTasks();
}

function paintTasks() {
  const now = Date.now() / 1000;
  const items = state.taskItems;
  const body = qs("#task-body");
  const groups = {};
  items.forEach((r) => {
    const k = taskBucket(r, now);
    (groups[k] = groups[k] || []).push(r);
  });
  // 「全部」视图里后端是按落定时间排历史的，混进待触发的行就没有方向了——每一堆自己按到期时间排
  ["overdue", "today", "week", "later"].forEach((k) =>
    groups[k] && groups[k].sort((a, b) => a.due_ts - b.due_ts));
  const order = ["overdue", "today", "week", "later", "history"].filter((k) => groups[k] && groups[k].length);
  const html = order.map((k) => {
    const [label, tone] = TASK_GROUP_LABEL[k];
    return `<div class="task-group">
      <div class="task-group-head"><span class="badge ${tone}">${label}</span>
        <span class="muted">${groups[k].length} 条</span></div>
      ${groups[k].map((r) => taskItem(r, now)).join("")}
    </div>`;
  }).join("") || `<div class="empty">${
    qs("#task-session").value || qs("#task-q").value.trim()
      ? "这个筛选下没有任务"
      : "还没有任务（对机器人说「五分钟后提醒我…」或「每天 18 点看看这个网站有没有变化」就会挂上）"
  }</div>`;
  // 每秒轮询都会重画这张列表：不记一下滚动位置，读到一半就被弹回顶部
  const top = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = top;

  const s = state.taskStats;
  const more = state.taskTotal > items.length;
  qs("#task-count").textContent = ` · 在排 ${s.pending || 0}（今天 ${s.today || 0} · 逾期 ${s.overdue || 0} · 周期 ${s.periodic || 0}）· 历史 ${
    (s.fired || 0) + (s.cancelled || 0) + (s.expired || 0) + (s.failed || 0)} 条`;
  qs("#task-more").classList.toggle("hidden", !more || items.length >= TASK_WINDOW);
  qs("#task-more-note").textContent = more
    ? items.length >= TASK_WINDOW
      ? `已显示 ${items.length} / ${state.taskTotal} 条 · 一次最多载 ${TASK_WINDOW} 条，用筛选缩小范围`
      : `已显示 ${items.length} / ${state.taskTotal} 条`
    : `共 ${state.taskTotal} 条`;
  const keep = state.taskLimits.keep_history_days, cap = state.taskLimits.max_rows;
  qs("#task-tip").textContent = (keep || cap)
    ? `已跑完/已取消/已过期/已失败的行保留 ${keep || "—"} 天，整表上限 ${cap || "—"} 行，超了从最旧的删起；在排的不参与清理，要它停下就点「取消」。`
    : "任务工具未挂载，历史清理入口暂时不可用。";
}

function loadMoreTasks() {
  state.taskLimit = Math.min((state.taskLimit || TASK_PAGE) + TASK_PAGE, TASK_WINDOW);
  reloadPage("task");
}

function openTaskEdit(id) {
  const it = state.taskItems.find((x) => Number(x.id) === Number(id));
  if (!it) return;
  const failed = it.status === "failed";
  openModal(
    `${failed ? "重排任务" : "改任务"} #${it.id}`,
    `<label class="field"><span>下一次到期</span>
       <input id="task-edit-due" class="input" type="datetime-local" value="${taskLocalInput(it.due_ts)}"></label>
     <label class="field"><span>指令</span>
       <input id="task-edit-instruction" class="input" value="${esc(it.instruction)}"></label>
     <p class="card-sub">${failed
       ? `这条连着失败 ${Number(it.fail_count) || 0} 次后停下了（${esc(it.last_error || "原因见日志")}），保存后会当成重新排上，失败计数清零。`
       : it.rule
         ? `这是「${esc(it.rule_label)}」的任务，改的是下一场的时间；后面每一场都跟着这个钟点走。`
         : "一次性任务，改完就到这个点跑。"} 改不了通知对象和会话——要换就取消再重排。</p>`,
    `<button class="btn ghost" data-action="modal-close">取消</button>
     <button class="btn primary" data-action="task-save" data-id="${esc(id)}">保存</button>`
  );
}

async function saveTask(id) {
  const it = state.taskItems.find((x) => Number(x.id) === Number(id));
  const due = qs("#task-edit-due").value;
  const instruction = qs("#task-edit-instruction").value.trim();
  if (!instruction) { toast("指令不能为空", "error"); return; }
  if (!due) { toast("时间没填对", "error"); return; }
  const body = { instruction };
  // 只把人真的动过的那一格发过去：逾期项预填的是已经过去的时间，改个错别字不该被「必须是将来」挡回来
  if (!it || due !== taskLocalInput(it.due_ts)) body.due_ts = Math.floor(new Date(due).getTime() / 1000);
  try {
    // 选择器给的是浏览器本地的那一刻，换成绝对时刻存库——服务器按 unix 秒比较，跨时区不吃亏
    await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "PUT", body });
    closeModal();
    toast(body.due_ts ? `已改到 ${taskDue(body.due_ts)}` : "已改指令", "success");
    reloadPage("task");
  } catch (err) { toast(err.message, "error"); }
}

async function cancelTask(id) {
  const it = state.taskItems.find((x) => Number(x.id) === Number(id));
  const ok = await confirmAction("取消任务",
    it && it.rule
      ? `取消 #${id} 这条「${it.rule_label}」任务（已跑 ${Number(it.fired_count) || 0} 场），后面不再跑。`
      : `取消 #${id} 后到点不会再跑任何东西。`,
    "取消任务");
  if (!ok) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    toast(`已取消 #${id}`, "success");
    reloadPage("task");
  } catch (err) { toast(err.message, "error"); }
}

async function deleteTask(id) {
  const ok = await confirmAction("删除记录",
    `把 #${id} 这条记录从表里删掉，删了查不回来。在排的任务要停下请用「取消」。`, "删除");
  if (!ok) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`已删除 #${id}`, "success");
    reloadPage("task");
  } catch (err) { toast(err.message, "error"); }
}

function openTaskPurge() {
  const s = state.taskStats;
  const sess = state.sessions.map((x) =>
    `<option value="${esc(x.id)}">${esc(x.label || labelFromKey(x.id))}</option>`).join("");
  openModal(
    "清理历史任务",
    `<label class="field"><span>删掉哪些</span><select id="task-purge-status" class="sel">
       <option value="">全部已结束（已跑完 ${s.fired || 0} · 已取消 ${s.cancelled || 0} · 已过期 ${s.expired || 0} · 已失败 ${s.failed || 0}）</option>
       <option value="fired">只删已跑完</option>
       <option value="cancelled">只删已取消</option>
       <option value="expired">只删已过期</option>
       <option value="failed">只删已失败</option>
     </select></label>
     <label class="field"><span>哪个会话</span><select id="task-purge-session" class="sel">
       <option value="">全部会话</option>${sess}</select></label>
     <label class="field"><span>再限定</span><select id="task-purge-days" class="sel">
       <option value="">不看时间</option>
       <option value="7">到期时间在 7 天以前</option>
       <option value="30">到期时间在 30 天以前</option>
       <option value="90">到期时间在 90 天以前</option>
     </select></label>
     <p class="card-sub">在排的任务不在清理范围里——它们要停下得逐条「取消」。</p>`,
    `<button class="btn ghost" data-action="modal-close">取消</button>
     <button class="btn danger" data-action="task-purge-run">删除</button>`
  );
}

async function purgeTasks() {
  const params = new URLSearchParams();
  const st = qs("#task-purge-status").value;
  const sid = qs("#task-purge-session").value;
  const days = qs("#task-purge-days").value;
  if (st) params.set("status", st);
  if (sid) params.set("session_id", sid);
  if (days) params.set("before_days", days);
  if (!params.toString()) { toast("至少选一个条件", "error"); return; }
  try {
    const r = await api(`/api/tasks?${params}`, { method: "DELETE" });
    closeModal();
    toast(`已删掉 ${r.removed || 0} 条历史任务`, "success");
    state.taskLimit = TASK_PAGE;
    reloadPage("task");
  } catch (err) { toast(err.message, "error"); }
}

async function sweepTasks() {
  const keep = state.taskLimits.keep_history_days || "—";
  const cap = state.taskLimits.max_rows || "—";
  const ok = await confirmAction("按保留期清理",
    `删掉落定超过 ${keep} 天的已结束任务，整表还超 ${cap} 行就再从最旧的删起；在排的不动。`,
    "开始清理");
  if (!ok) return;
  try {
    const r = await api("/api/tasks/sweep", { method: "POST" });
    toast(`到期删 ${r.aged || 0} · 超限删 ${r.over || 0}（剩 ${r.total || 0} 条）`, "success");
    state.taskLimit = TASK_PAGE;
    reloadPage("task");
  } catch (err) { toast(err.message, "error"); }
}
