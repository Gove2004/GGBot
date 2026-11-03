"use strict";

/* ================= 状态 / 统计 ================= */
/* 本轮窗口：倒计时的三种读数——秒、分:秒、时:分 */
function remainText(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  const p = (x) => String(x).padStart(2, "0");
  if (n < 60) return [String(n), "s"];
  if (n < 3600) return [`${Math.floor(n / 60)}:${p(n % 60)}`, "分:秒"];
  return [`${Math.floor(n / 3600)}:${p(Math.floor((n % 3600) / 60))}`, "时:分"];
}

/* 值班带：轨道是「这一轮的名义窗口」，填充是走过的比例。
 * 回复续期把截止点往后推的时候面板拿不到窗口起点，所以续出来的那截只显示在右侧标签上。 */
function renderDuty(s, online) {
  const base = Math.max(1, Math.round(Number(online ? s.online_duration_sec : s.offline_interval_sec) || 0));
  const remain = Math.max(0, Math.round(Number(s.remaining_sec) || 0));
  const elapsed = Math.max(0, base - remain);
  const pct = Math.min(100, Math.round((elapsed / base) * 1000) / 10);
  const jitter = Math.round((Number(online ? s.online_duration_jitter : s.offline_interval_jitter) || 0) * 100);
  const over = Math.max(0, remain - base);
  qs("#duty-label").textContent = online ? "本轮在线窗口" : "本轮离线窗口";
  qs("#duty-window").textContent = `${fmtAgo(base)}${jitter ? ` ±${jitter}%` : ""}${over ? ` · 续期 +${fmtAgo(over)}` : ""}`;
  qs("#duty-fill").style.width = `${pct}%`;
  qs("#duty-now").style.left = `${pct}%`;
  qs("#duty-elapsed").textContent = `已走 ${fmtAgo(elapsed)}`;
  qs("#duty-left").textContent = remain > 0 ? `还剩 ${fmtAgo(remain)}` : "到点";
  const track = qs("#duty-track");
  if (track) track.title = `名义窗口 ${fmtAgo(base)}，实际每次按 ±${jitter || 0}% 抖动；续期推后的时间不进轨道`;
}

async function refreshStatus() {
  const s = await api("/api/status");
  state.status = s;
  const online = s.state === "online";
  const conn = qs("#conn-pill");
  conn.className = `pill ${online ? "on" : "off"}`;
  conn.innerHTML = `<i></i>${online ? "在线" : "离线"}`;
  const waiting = s.waiting_count || 0;
  const pend = qs("#pending-pill");
  pend.className = `pill ${waiting > 0 ? "warn" : "ghost"}`;
  pend.textContent = `${waiting} 待处理`;

  // 离线态：侧栏发光边褪成中性（hero 那一侧靠下面摘掉 online 类转成结构蓝），一眼看着不在岗
  qs("#app").classList.toggle("offline", !online);
  qs("#side-dot").className = `dot${online ? "" : " off"}`;
  qs("#side-state").textContent = online ? "在线" : "离线";
  qs("#side-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#side-pending").textContent = waiting;

  qs("#dash-hero").classList.toggle("online", online);
  qs("#dash-dot").className = `state-dot ${online ? "on" : "off"}`;
  qs("#dash-state").textContent = online ? "在线" : "离线";
  qs("#dash-event").textContent = s.next_event || "等待下一次上线";
  const [big, unit] = remainText(s.remaining_sec);
  qs("#dash-remain").textContent = s.remaining_sec != null ? big : "—";
  qs("#dash-remain-unit").textContent = unit;
  renderDuty(s, online);
  const live = (s.sessions && s.sessions[0]) || null;
  qs("#dash-caption").textContent =
    `${reasonLabel(live ? live.reason : "idle")} · ${s.online_sessions || 0} 个会话在线 · ${waiting} 条积压`;

  qs("#m-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#m-pending").textContent = s.waiting_count != null ? s.waiting_count : "—";
  renderDashSessions();
}

async function refreshStats() {
  const d = await api("/api/stats");
  state.stats = d;
  qs("#m-online").textContent = d.online_sessions != null ? d.online_sessions : "—";
  qs("#m-pending").textContent = d.waiting != null ? d.waiting : "—";
  qs("#m-memories").textContent = d.memories != null ? d.memories : "—";
  qs("#m-sessions").textContent = d.sessions != null ? d.sessions : "—";
  qs("#m-today").textContent = d.messages_today != null ? d.messages_today : "—";
  const rt = d.runtime || {};
  const uptime = qs("#rt-uptime");
  if (uptime) uptime.textContent = rt.uptime_sec != null ? `运行 ${fmtUptime(rt.uptime_sec)}` : "运行时长 —";
  qs("#rt-sessions").textContent = fmtNum(rt.sessions);
  qs("#rt-received").textContent = fmtNum(rt.received);
  qs("#rt-sent").textContent = fmtNum(rt.sent);
  qs("#rt-tools").textContent = fmtNum(rt.tool_calls);
  qs("#rt-llm").textContent = fmtNum(rt.llm_calls);
  qs("#rt-vision").textContent = fmtNum(rt.vision_calls);
  qs("#rt-tokens").textContent = fmtNum(rt.tokens);
  qs("#rt-prompt").textContent = fmtNum(rt.prompt_tokens);
  qs("#rt-completion").textContent = fmtNum(rt.completion_tokens);
  const promptToks = Number(rt.prompt_tokens) || 0;
  const cachedToks = Number(rt.cached_tokens) || 0;
  const cache = qs("#rt-cache");
  if (cache) {
    cache.textContent = promptToks > 0 ? `${Math.round((cachedToks / promptToks) * 100)}%` : "—";
    cache.title = `缓存命中 ${fmtNum(cachedToks)} / 输入 ${fmtNum(promptToks)} tokens`;
  }
  const priced = rt.cost_text && rt.cost_text !== "-";
  qs("#rt-cost").textContent = priced ? rt.cost_text : "—";
  const hint = qs("#rt-price-hint");
  if (hint) hint.textContent = priced ? "按 llm.pricing 单价估算" : "未配置单价 · 见设置页";
  renderEnv(d.env);
}

function renderEnv(env) {
  if (!env) return;
  const side = qs("#side-model");
  if (side) {
    side.textContent = env.chat_model || "配置文件驱动";
    side.title = `${env.chat_model} @ ${env.chat_host}`;
  }
  const set = (id, text, title) => {
    const el = qs(id);
    if (!el) return;
    el.textContent = text == null ? "—" : String(text);
    if (title !== undefined) el.title = title;
  };
  set("#env-chat", env.chat_model, env.chat_host);
  set("#env-chat-host", env.chat_host);
  set("#env-vision", env.vision_model || "未启用");
  set("#env-vision-host", env.vision_model ? env.vision_host : "vision.enabled=false 或未填模型");
  set("#env-memory", env.memory_mode === "向量" ? "向量检索" : "关键词检索");
  set("#env-memory-model", env.memory_model || "未配置向量服务，记忆改用关键词检索");
  set("#env-log", env.log_level);
  set("#env-log-file", `${env.temp}/logs/ggbot.log`);
  set("#env-db", env.db);
  set("#env-temp", env.temp);
}

function renderDashSessions() {
  const body = qs("#dash-sessions");
  if (!body) return;
  const live = {};
  ((state.status && state.status.sessions) || []).forEach((x) => { live[x.sid] = x; });
  const row = (sid, l, pending) => `<tr>
      <td>${sessChip(sid)}</td>
      <td>${stateBadge(l.state)}<span class="cell-note">${esc(reasonLabel(l.reason))}</span></td>
      <td class="col-num ${pending ? "warn" : "muted"}">${pending == null ? "—" : pending}</td>
      <td class="col-num mono">${l.remaining_sec != null ? Math.max(0, Math.round(l.remaining_sec)) + "s" : "—"}</td>
      <td class="muted small">${fmtAgo(l.idle_sec)}</td>
    </tr>`;
  let rows = state.sessions.slice(0, 8).map((s) => {
    const l = (s.live && s.live.state) ? s.live : (live[s.id] || { state: "offline", reason: "idle", remaining_sec: 0, idle_sec: null });
    return row(s.id, l, s.waiting_count || 0);
  });
  if (!rows.length) {
    rows = ((state.status && state.status.sessions) || []).slice(0, 8).map((l) => row(l.sid, l, null));
  }
  body.innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">暂无会话</td></tr>';
}

async function refreshDashRecent() {
  const rows = await api("/api/messages?limit=8");
  const box = qs("#dash-recent");
  box.innerHTML = (rows || []).map((m) => `<div class="mini-item">
    <span class="mini-role ${m.role === "assistant" ? "assistant" : ""}"></span>
    <div class="mini-main">
      <div class="mini-meta"><span>${sessChip(m.skey || m.session_id, "tiny")}</span><span>${esc(fmtTs(m.ts))}</span></div>
      <div class="mini-text" title="${esc(m.content)}">${esc(m.content || "（无文本）")}</div>
    </div>
  </div>`).join("") || '<div class="empty">暂无消息</div>';
}

