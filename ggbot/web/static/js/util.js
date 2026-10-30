"use strict";

/* ================= 工具 ================= */
const qs = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const PAGE_META = {
  dash: ["仪表盘", "运行概览与实时状态"],
  sessions: ["会话与上下文", "每个群 / 私聊的历史与消息"],
  messages: ["消息流", "跨会话的最近消息记录"],
  memory: ["向量记忆", "长期记忆的语义检索与维护"],
  settings: ["设置", "配置热更新，写回 config.json"],
  permissions: ["权限", "发送者等级与工具调用门槛"],
  tools: ["工具", "一切皆 Tool · 独立配置 / 持久化数据 / 动态挂载"],
  logs: ["运行日志", "等级是门槛（该级及以上）· 会话与内容在全量日志里筛"],
  reminders: ["提醒", "会话里挂着的定时提醒 · 到点由巡检循环原样发出，不再问模型"],
};

const REASON = {
  initial: "刚启动",
  scheduled: "自然上线",
  "wake-on-at": "被@唤醒",
  timeout: "自然下线",
  "reply-renew": "聊天续期",
  idle: "静默",
};

const state = {
  page: "dash",
  status: null,
  stats: null,
  sessions: [],
  selectedSession: null,
  memItems: [],
  memoryQuery: "",
  memoryUseSearch: false,
  loading: {},
  timers: [],
  confirmResolve: null,
  tools: [],
  toolCat: "",
  actors: [],
  levelNames: {},
  defaultLevel: 2,
  maxLevel: 5,
  lockedLevel: 6,
  logsRendered: false,
};

