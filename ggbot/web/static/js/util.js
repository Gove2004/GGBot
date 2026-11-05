"use strict";

/* ================= 工具 ================= */
const qs = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

/* 顺序跟侧栏的四组一致：现场 → 对话 → 知识 → 配置。
   查表用的是键，重排不影响行为，只是看代码时不用在两处来回跳。 */
const PAGE_META = {
  dash: ["仪表盘", "运行概览与实时状态"],
  logs: ["运行日志", "等级是门槛（该级及以上）· 会话与内容在全量日志里筛"],
  sessions: ["会话与上下文", "每个群 / 私聊的历史与消息"],
  schedule: ["日程", "一次性与周期日程 · 到点由巡检循环原样发出，不再问模型"],
  memory: ["记忆", "档案会被新事实取代 · 事件很久不用就淡出"],
  emojis: ["表情包", "本地素材库 · 目录里有什么就能发什么"],
  persona: ["人设", "系统提示词 · 所有会话共用的那一段"],
  settings: ["设置", "配置热更新，写回 config.json"],
  permissions: ["权限", "发送者等级与工具调用门槛"],
  tools: ["工具", "一切皆 Tool · 独立配置 / 持久化数据 / 动态挂载"],
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
  memLimits: {},
  memUsage: null,
  memoryQuery: "",
  memoryUseSearch: false,
  emojiItems: [],
  emojiMode: "",
  emojiMax: 0,
  emojiMaxBytes: 0,
  loading: {},
  timers: [],
  confirmResolve: null,
  tools: [],
  toolCat: "",
  actors: [],
  /* 这些数一律由后端给（等级契约、记忆契约、人设字数上限），前端不留第二份手写的值：
     改了后端口径而面板没跟上，只会是「这里显示空/0」这种看得见的问题，不会静默错。 */
  levelNames: {},
  defaultLevel: null,
  maxLevel: null,
  lockedLevel: null,
  lockedName: "",
  logsRendered: false,
  personaMax: 0,
  memKinds: {},
  memImpMin: 0,
  memImpMax: 0,
  schItems: [],
  schTotal: 0,
  schStats: {},
  schLimits: {},
  schLimit: 0,
};

