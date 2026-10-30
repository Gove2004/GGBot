"use strict";

/* ================= 状态 / 统计 ================= */
async function refreshStatus() {
  const s = await api("/api/status");
  state.status = s;
  const online = s.state === "online";
  const conn = qs("#conn-pill");
  conn.className = `pill ${online ? "on" : "off"}`;
  conn.innerHTML = `<i></i>${online ? "在线" : "离线"}`;
  const pending = s.pending_count || 0;
  const pend = qs("#pending-pill");
  pend.className = `pill ${pending > 0 ? "warn" : "ghost"}`;
  pend.textContent = `${pending} 待处理`;

  qs("#side-dot").className = `dot${online ? "" : " off"}`;
  qs("#side-state").textContent = online ? "在线" : "离线";
  qs("#side-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#side-pending").textContent = pending;

  qs("#dash-hero").classList.toggle("online", online);
  qs("#dash-dot").className = `state-dot ${online ? "on" : "off"}`;
  qs("#dash-state").textContent = online ? "在线" : "离线";
  qs("#dash-event").textContent = s.next_event || "等待下一次上线";
  qs("#dash-remain").textContent = s.remaining_sec != null ? Math.max(0, Math.round(s.remaining_sec)) : "—";
  const base = Number(online ? s.online_duration_sec : s.offline_interval_sec) || 1;
  const remain = Math.max(0, Number(s.remaining_sec) || 0);
  const prog = qs("#dash-progress");
  prog.max = Math.max(1, Math.round(base));
  prog.value = Math.max(0, Math.min(base, base - remain));
  const live = (s.sessions && s.sessions[0]) || null;
  qs("#dash-caption").textContent =
    `${reasonLabel(live ? live.reason : "idle")} · ${s.online_sessions || 0} 个会话在线 · ${pending} 条积压`;

  qs("#m-online").textContent = s.online_sessions != null ? s.online_sessions : "—";
  qs("#m-pending").textContent = s.pending_count != null ? s.pending_count : "—";
  renderDashSessions();
}

async function refreshStats() {
  const d = await api("/api/stats");
  state.stats = d;
  qs("#m-online").textContent = d.online_sessions != null ? d.online_sessions : "—";
  qs("#m-pending").textContent = d.pending != null ? d.pending : "—";
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
    return row(s.id, l, s.pending_count || 0);
  });
  if (!rows.length) {
    rows = ((state.status && state.status.sessions) || []).slice(0, 8).map((l) => row(l.sid, l, null));
  }
  body.innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">暂无会话</td></tr>';
}

