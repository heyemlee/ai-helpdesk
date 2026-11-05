/* DOM 小工具。

   页面上大部分内容来自数据库与模型（表格单元、SQL、报错、问题原文），
   全都是不可信文本。这里的约定是：**视图只用 html`` 拼标记，只用 setHtml 写入**。
   html`` 对每个插值默认转义；只有它自己产出的片段、以及下面 icon() 这种
   完全由本文件写死的标记才原样输出。于是「忘了转义」不是可能犯的错——
   要原样输出必须显式写 raw()，而 raw() 在整个前端只该出现在这里。 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (char) => ESCAPES[char]);

class SafeHtml {
  constructor(markup) { this.markup = markup; }
  toString() { return this.markup; }
}

const raw = (markup) => new SafeHtml(String(markup));

function render(value) {
  // 布尔值不输出：`${cond ? html`…` : ''}` 之外偶尔写成 `${cond && …}` 时不该冒出一个 "false"。
  // 需要 "true"/"false" 字样的属性（aria-selected 之类）自己用 String() 转。
  if (value == null || value === false || value === true) return '';
  if (value instanceof SafeHtml) return value.markup;
  if (Array.isArray(value)) return value.map(render).join('');
  return esc(value);
}

export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, index) => { out += render(value) + strings[index + 1]; });
  return new SafeHtml(out);
}

/* 整块重绘会把焦点所在的按钮也换掉。用键盘操作的人点完「下一页」，焦点不能掉回页面顶上——
   所以重绘前记下焦点在谁身上（id，或 data-action 加上它带的那几个 data-*），
   重绘后在新的标记里找回同一个控件。

   用来认控件的属性必须是**重绘前后不变的**。data-page 不在其中：翻页按钮带的是
   「要去的页码」，翻完就变了，拿它认的话每翻一页焦点都丢——翻页按钮用 data-which
   （prev / next）认。 */
const FOCUS_KEYS = [
  'lang', 'op', 'tab', 'step', 'scope', 'key', 'index', 'trace', 'verdict', 'which', 'example', 'detail',
  // 数据页：分布图里的一个取值、时间轴上的一根柱子、字段开关
  'column', 'value', 'start', 'name',
];

function focusSelector(root) {
  const active = document.activeElement;
  if (!active || active === document.body || !root.contains(active)) return '';
  if (active.id) return '#' + CSS.escape(active.id);
  const data = active.dataset || {};
  if (data.action) {
    return `[data-action="${CSS.escape(data.action)}"]` + FOCUS_KEYS
      .filter((key) => data[key] !== undefined)
      .map((key) => `[data-${key}="${CSS.escape(data[key])}"]`)
      .join('');
  }
  if (data.input) return `[data-input="${CSS.escape(data.input)}"]`;
  // 换页后焦点停在页面标题上；标题跟着内容重画时也要留得住
  if (data.role) return `[data-role="${CSS.escape(data.role)}"]`;
  // 折叠块的标题行：展开它会触发重画（原始响应展开时才序列化）
  const fold = active.tagName === 'SUMMARY' ? active.parentElement : null;
  if (fold && fold.dataset && fold.dataset.detail) {
    return `details[data-detail="${CSS.escape(fold.dataset.detail)}"] > summary`;
  }
  return '';
}

export function setHtml(element, content) {
  const selector = focusSelector(element);
  element.innerHTML = render(content);
  if (!selector) return;
  let next = element.querySelector(selector);
  // 翻到最后一页，「下一页」就禁用了：焦点交给它旁边还能用的那个，不掉回页面顶上
  if (next && next.disabled && next.parentElement) {
    next = next.parentElement.querySelector('button:not([disabled])');
  }
  if (next && !next.disabled) next.focus({ preventScroll: true });
}

export const $ = (id) => document.getElementById(id);

/* 图标：路径写死在这里，名字不认识就不画。 */
const ICONS = {
  ask: '<path d="M4 5h16v11H9l-5 4z"/>',
  data: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  history: '<path d="M4 12a8 8 0 1 0 2.6-5.9L4 8.5M4 4v4.5h4.5M12 8v4.5l3 2"/>',
  send: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16m0 4v-4h-4"/>',
  settings: '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.2"/>',
  warn: '<path d="M10.4 4.6 2.9 17.8A1.8 1.8 0 0 0 4.5 20.5h15a1.8 1.8 0 0 0 1.6-2.7L13.6 4.6a1.8 1.8 0 0 0-3.2 0Z"/><path d="M12 9.5v4.2M12 16.8v.2"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="m8.8 8.8 6.4 6.4m0-6.4-6.4 6.4"/>',
  empty: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5M8.5 11h5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  back: '<path d="m14 6-6 6 6 6"/>',
  learn: '<path d="M3 8.5 12 4l9 4.5-9 4.5z"/><path d="M7 11v4.5c0 1.2 2.2 2.5 5 2.5s5-1.3 5-2.5V11"/>',
  logo: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  columns: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M9.5 5v14M14.5 5v14"/>',
  table: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16M4 14.5h16M10 10v9"/>',
  chart: '<path d="M5 20V10m7 10V4m7 16v-7"/>',
  key: '<circle cx="8" cy="15" r="3.5"/><path d="m10.6 12.4 8.4-8.4M16 7l2.5 2.5M13.5 9.5l2 2"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  open: '<path d="M14 5h5v5M19 5l-8 8M11 6H7a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-4"/>',
  home: '<path d="M4 11.5 12 5l8 6.5M6.5 10v9h11v-9"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  reply: '<path d="M9 7 4 12l5 5M4 12h10a6 6 0 0 1 6 6v1"/>',
  arrowUpRight: '<path d="M8 16 16 8M9 8h7v7"/>',
  thumbUp: '<path d="M7 11v9H4.5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zm0 0 4-7.5a2 2 0 0 1 2.6 2.3L13 9h5.2a2 2 0 0 1 2 2.4l-1.3 7a2 2 0 0 1-2 1.6H7"/>',
  thumbDown: '<path d="M7 13V4H4.5a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1zm0 0 4 7.5a2 2 0 0 0 2.6-2.3L13 15h5.2a2 2 0 0 0 2-2.4l-1.3-7a2 2 0 0 0-2-1.6H7"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.7 2.2c-.8.5-1.3 1-1.3 1.9M12 16.6v.2"/>',
  hash: '<path d="M9.5 4 7.5 20M16.5 4l-2 16M4.5 9h16M3.5 15h16"/>',
};

export function icon(name, extraClass = '') {
  const paths = ICONS[name];
  if (!paths) return '';
  return raw(
    `<svg class="icon ${esc(extraClass)}" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`,
  );
}

/* 给读屏的播报。内容区是整块重绘的：一个带着内容被整个换进来的 aria-live 区域
   不会被念出来（念的是「变化」，不是「出现」）。所以播报走这一个常驻的区域。 */
let announceTimer;
export function announce(message) {
  const element = $('announcer');
  if (!element) return;
  element.textContent = '';
  clearTimeout(announceTimer);
  // 先清空再写：连着两条一样的话（两次都「没有匹配的记录」）也各念一次
  announceTimer = setTimeout(() => { element.textContent = String(message || ''); }, 60);
}

let toastTimer;
export function toast(message) {
  const element = $('toast');
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 2400);
}

/* 复制。navigator.clipboard 只在安全上下文可用（localhost 算，局域网 http 不算），
   所以留一条 textarea 的老路。返回是否成功，文案由调用方给。 */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* 落到下面 */ }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.append(area);
  area.select();
  let done = false;
  try { done = document.execCommand('copy'); } catch (_) { done = false; }
  area.remove();
  return done;
}

/* 事件委托：视图整块重绘，监听挂在不变的根上，按 data-action 分发。 */
export function delegate(root, type, handlers) {
  root.addEventListener(type, (event) => {
    const target = event.target.closest('[data-action]');
    if (!target || !root.contains(target)) return;
    const handler = handlers[target.dataset.action];
    if (handler) handler(target, event);
  });
}
