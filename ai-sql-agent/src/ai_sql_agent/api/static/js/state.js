/* 页面状态与本地偏好。

   落 localStorage 的只有偏好（语言、学习模式、服务地址、已提交的反馈）。
   两样东西刻意不落盘：API Key 只在 sessionStorage（关掉标签页就没了），
   查询结果只在内存（历史页说的「本次会话缓存」就是这份）——
   结果里是业务数据，浏览器不该替服务端多存一份副本。 */

const STORAGE_KEY = 'ai-sql-console/v2';
const LEGACY_KEY = 'ai-sql-console/v1';
// 会话标识不在其中：页面上的对话不跨刷新保留，服务端的会话也就不该悄悄延续——
// 否则刷新后看着是一个空白的新对话，问出来的却是「根据之前的查询结果…」。
const PERSISTED = ['lang', 'learning', 'base', 'userId', 'longTerm', 'transport', 'feedback', 'explorer'];
const MAX_FEEDBACK_ENTRIES = 200;

/* crypto.randomUUID 只在安全上下文里有（localhost 算，局域网 http 不算），留一条退路。 */
export const uid = () => (
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID().replace(/-/g, '')
    : Date.now().toString(16) + Math.random().toString(16).slice(2, 14)
);
export const newSessionId = () => 's-' + uid().slice(0, 8);

export const state = {
  lang: 'en',              // 面向美国业务，默认英文；切过一次就记住
  learning: false,         // 学习模式，默认关
  base: '',                // 服务地址，留空 = 页面所在的这台服务
  userId: '',              // 「以谁的身份」：留空 = 用 API Key 自己绑定的身份
  sessionId: newSessionId(),   // 每次打开页面一个新会话；「新问题」也会换一个
  longTerm: false,
  transport: 'auto',       // auto | query | stream
  feedback: {},            // trace_id -> 已提交的判定
  explorer: { insights: true },   // 数据页的显示偏好：表页上的概览开不开

  apiKey: '',
  live: null,              // null 还没探测 / true / false
  identity: null,          // /catalog 回来的身份
  service: null,           // /catalog 回来的服务能力
};

/* 浏览器禁用了站点存储时，连读 window.localStorage 这个属性本身都会抛错。
   存不了不该让页面起不来——偏好只在本次有效而已。 */
function readJson(kind, key) {
  try { return JSON.parse(window[kind].getItem(key) || 'null'); } catch (_) { return null; }
}

function load() {
  const saved = readJson('localStorage', STORAGE_KEY);
  if (saved && typeof saved === 'object') {
    PERSISTED.forEach((key) => { if (key in saved) state[key] = saved[key]; });
  } else {
    // 上一版页面的偏好：能沿用的沿用，免得升级后要重新填一遍连接设置
    const legacy = readJson('localStorage', LEGACY_KEY);
    if (legacy && typeof legacy === 'object') {
      if (typeof legacy.base === 'string') state.base = legacy.base;
      if (typeof legacy.userId === 'string') state.userId = legacy.userId;
      // 语言不沿用：上一版是跟着浏览器语言自动选的，不代表用户选过
      state.learning = Boolean(legacy.explain);
      state.longTerm = Boolean(legacy.longTerm);
    }
  }
  if (!['en', 'zh'].includes(state.lang)) state.lang = 'en';
  if (!['auto', 'query', 'stream'].includes(state.transport)) state.transport = 'auto';
  if (!state.feedback || typeof state.feedback !== 'object') state.feedback = {};
  if (!state.explorer || typeof state.explorer !== 'object') state.explorer = { insights: true };
  try {
    state.apiKey = sessionStorage.getItem(STORAGE_KEY + '/key')
      || sessionStorage.getItem(LEGACY_KEY + '/key') || '';
  } catch (_) { state.apiKey = ''; }
}

export function save() {
  const entries = Object.entries(state.feedback);
  if (entries.length > MAX_FEEDBACK_ENTRIES) {
    state.feedback = Object.fromEntries(entries.slice(-MAX_FEEDBACK_ENTRIES));
  }
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(Object.fromEntries(PERSISTED.map((key) => [key, state[key]]))),
    );
    if (state.apiKey) sessionStorage.setItem(STORAGE_KEY + '/key', state.apiKey);
    else sessionStorage.removeItem(STORAGE_KEY + '/key');
  } catch (_) { /* 隐私模式下存不了：偏好只在本次有效，不影响使用 */ }
}

/* 发给 /query 的 user_id。优先用服务端解析出来的身份——员工不该需要知道
   自己的 user_id 是什么；只有手动指定了「以谁的身份」才用指定的。 */
export function effectiveUserId() {
  return state.userId || state.identity?.user_id || 'anonymous';
}

const listeners = new Map();
export function on(topic, listener) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(listener);
}
export function emit(topic, payload) {
  (listeners.get(topic) || []).forEach((listener) => listener(payload));
}

/* 本次会话里拿到过的完整结果：trace_id -> { result, question, capturedAt }。只在内存。 */
export const resultCache = new Map();

load();
