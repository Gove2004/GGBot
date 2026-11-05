"use strict";

/* ================= 页面读数 ================= */
/* 面板的读只剩两条路：
   /api/shell        —— 顶栏、侧栏与仪表盘的实时读数（poll.js 每秒拉一次）
   /api/page/<name>  —— 当前页的完整快照，一次往返画完整页
   页面上的筛选控件就是这一页的请求参数：筛选变了重拉这一页，不再各拉各的接口。
   写操作照旧按资源走（POST/PUT/DELETE），改完由调用方 reloadPage() 把这一页重新拉回来。 */

/* 只有这几页的数据会自己变，才值得挂着定时器；其余页面进出才拉。
   日志要跟得上滚动，仪表盘和日程的倒计时/逾期得走着看。 */
const PAGE_LIVE = { dash: 5000, schedule: 5000, logs: 1000 };

/* 每页的参数从它自己的控件读出来：轮询和手工刷新拉的是同一份口径，不会两套筛法。
   日志一次多少行不由前端报数——后端 LOG_LINES_DEFAULT 说了算，前端要筛就只筛级别和会话。 */
const PAGE_PARAMS = {
  logs: () => {
    const p = { level: qs("#log-level").value };
    const sess = qs("#log-session").value.trim();
    const q = qs("#log-q").value.trim();
    if (sess) p.session = sess;
    if (q) p.q = q;
    return p;
  },
  memory: () => {
    const p = {};
    const sid = qs("#mem-session").value;
    const kind = qs("#mem-kind").value;
    if (sid) p.session_id = sid;
    if (state.memoryUseSearch && state.memoryQuery) p.q = state.memoryQuery;
    else if (kind) p.kind = kind;
    return p;
  },
  schedule: () => {
    // 「载入更多」是把窗口撑大（state.schLimit），不是翻页：轮询和刷新都跟着这个窗口走
    const p = {
      status: qs("#sch-status").value,
      limit: String(Math.min(state.schLimit || SCH_PAGE, SCH_WINDOW)),
    };
    const sid = qs("#sch-session").value;
    const q = qs("#sch-q").value.trim();
    if (sid) p.session_id = sid;
    if (q) p.q = q;
    return p;
  },
};

/* 有的页不能想拉就拉：人设页正编辑的时候，一次自动重载会把没保存的改动冲掉。
   带 params 的调用（刷新按钮、写完回读）是明确要拉的，不受这条限制。 */
const PAGE_HOLD = {
  persona: () => personaDirty(),
};

/* 一页一个渲染入口；后端给了哪份数据就画哪块，共用的那份（sessions）由 setSessions 一次喂饱 */
const PAGE_RENDER = {
  dash: (d) => { setSessions(d.sessions); renderDashRecent(d.recent); },
  logs: (d) => renderLogs(d.logs),
  sessions: (d) => { setSessions(d.sessions); renderSessionsPage(); },
  schedule: (d) => { setSessions(d.sessions); renderSchedule(d.reminders); },
  memory: (d) => { setSessions(d.sessions); renderMemories(d.memories); },
  emojis: (d) => renderEmojis(d.emojis),
  persona: (d) => applyPersona(d.system_prompt),
  settings: (d) => renderSettings(d.settings),
  permissions: (d) => renderPermissions(d.permissions, d.tools, d.levels),
  // 工具页可能比权限页先打开，门槛要显示成「L4 心腹」就得自己带上等级契约
  tools: (d) => { applyLevelContract(d.levels); renderToolsPage(d.tools); },
};

async function loadPage(name, params) {
  if (!params && PAGE_HOLD[name] && PAGE_HOLD[name]()) return;
  const qs2 = new URLSearchParams(PAGE_PARAMS[name] ? PAGE_PARAMS[name]() : undefined);
  Object.entries(params || {}).forEach(([k, v]) => qs2.set(k, v));
  const tail = qs2.toString();
  const d = await api(`/api/page/${name}${tail ? `?${tail}` : ""}`);
  PAGE_RENDER[name](d);
}

/* 重新拉当前页（或指定页）。写操作之后调它，页面就和后端一致，不用各模块自己拼该刷哪几块。 */
function reloadPage(name, params) {
  const page = name || state.page;
  return guarded(`page:${page}`, () => loadPage(page, params));
}
