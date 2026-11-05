/* 入口：启动、路由、顶栏。

   三个一级入口对应三个视图，用 hash 路由（#/ask、#/data、#/data/<库>、#/data/<库>/<表>、
   #/history），所以刷新和「把这张表的链接发给同事」都能回到同一个地方，服务端不需要
   为此加路由。数据页的地址后面还可以带一段 ?…（搜索词、筛选、排序，见 dataRoute.js）。 */

import { api } from './api.js';
import { loadCatalog } from './catalog.js';
import { $ } from './dom.js';
import { loadHistory, resetHistory } from './historyStore.js';
import { applyStaticText, t } from './i18n.js';
import { emit, on, save, state } from './state.js';
import { focusComposer, initAsk, prefill, showAsk } from './views/ask.js';
import { initExplore, showData, takePrefill } from './views/explore.js';
import { initHistory, showHistory } from './views/history.js';
import { initSettings, openSettings } from './views/settings.js';

const VIEWS = ['ask', 'data', 'history'];
let current = null;
let shown = null;             // 当前显示的那条路由（视图 + 库 + 表）
let retryTimer = null;

/* hash → 路由。不认识的 hash 返回 null：那不是在导航（比如页内锚点），页面留在原地。
   以前不认识的一律当成「去提问页」，于是任何一个 #锚点 都会把人从数据页带走。 */
function parseRoute() {
  const hash = location.hash;
  if (hash !== '' && hash !== '#' && !hash.startsWith('#/')) return null;
  // 问号后面是视图自己的状态，不参与分段：表名里真有问号的话，链接里它是转义过的
  const mark = hash.indexOf('?');
  const path = mark === -1 ? hash : hash.slice(0, mark);
  const query = mark === -1 ? '' : hash.slice(mark + 1);
  let parts;
  try {
    parts = path.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  } catch (_) {
    return null;
  }
  if (parts[0] === 'data') {
    return { view: 'data', database: parts[1] || '', table: parts[2] || '', query };
  }
  if (parts[0] === 'history') return { view: 'history' };
  if (parts[0] === 'ask' || parts[0] === '') return { view: 'ask' };
  return null;
}

function setTitle(view) {
  document.title = `${t('nav.' + view)} · AI SQL Agent`;
}

function route({ initial = false } = {}) {
  const target = parseRoute() || shown || { view: 'ask' };
  // 同一个视图里换了内容也算换页：从目录点进一张表、顺着字段关联跳到另一张表
  const changedPage = !shown || ['view', 'database', 'table'].some((key) => (shown[key] || '') !== (target[key] || ''));
  current = target.view;
  shown = target;

  VIEWS.forEach((view) => { $('view-' + view).hidden = view !== target.view; });
  document.querySelectorAll('.primary-nav a').forEach((link) => {
    if (link.dataset.route === target.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  setTitle(target.view);

  if (target.view === 'data') showData(target.database, target.table, target.query || '');
  if (target.view === 'history') showHistory();
  if (target.view === 'ask') {
    showAsk();
    const text = takePrefill();
    if (text) prefill(text);
  }

  if (initial || !changedPage) return;
  // 新的一页从头看起。只在换视图时回顶的话，在目录下方点开一张表，
  // 打开的表页会停在半中间，标题和标签页都在屏幕外面。
  window.scrollTo(0, 0);
  // 换了页面：把焦点交给新页面的标题，读屏会念出到了哪里，Tab 也从这里往下走
  const title = $('view-' + target.view).querySelector('[data-role="view-title"]');
  if (title && !(target.view === 'ask' && document.activeElement && document.activeElement.id === 'askInput')) {
    title.focus({ preventScroll: true });
  }
}

function syncTopbar() {
  const learn = $('btnLearn');
  learn.setAttribute('aria-checked', String(state.learning));
  learn.title = t('learn.toggleHint');
  $('btnLang').textContent = state.lang === 'en' ? '中文' : 'EN';
  $('btnLang').setAttribute('aria-label', t('settings.switchLanguage'));

  const conn = $('connStatus');
  conn.dataset.state = state.live === null ? 'checking' : state.live ? 'ok' : 'down';
  conn.querySelector('.conn-text').textContent = t(
    state.live === null ? 'conn.checking' : state.live ? 'conn.ok' : 'conn.down',
  );
}

async function checkHealth() {
  const was = state.live;
  try {
    // /health 不走鉴权：它只回答「服务在不在」，Key 对不对由后面的接口说
    await api.health();
    state.live = true;
  } catch (_) {
    state.live = false;
  }
  syncTopbar();
  // 连上了、断开了：提问页首页据此显示或收起那条「服务连不上」的提示
  if (was !== state.live) emit('health');

  clearTimeout(retryTimer);
  if (!state.live) {
    retryTimer = setTimeout(checkHealth, 15000);
  } else if (was === false) {
    loadCatalog({ refresh: true });       // 断了又连上：目录重读一遍
  }
}

function boot() {
  applyStaticText();
  initAsk();
  initExplore();
  initHistory();
  initSettings();
  syncTopbar();
  route({ initial: true });

  window.addEventListener('hashchange', () => route());

  $('btnLearn').addEventListener('click', () => {
    state.learning = !state.learning;
    save();
    emit('learning');
  });
  $('btnLang').addEventListener('click', () => {
    state.lang = state.lang === 'en' ? 'zh' : 'en';
    save();
    emit('lang');
  });
  $('btnSettings').addEventListener('click', openSettings);

  // 「跳到主要内容」：把焦点交给内容区，不改地址栏。页面用 hash 做路由，
  // 让它按普通锚点去跳的话，地址就成了 #main。
  document.querySelector('.skip-link').addEventListener('click', (event) => {
    event.preventDefault();
    const main = $('main');
    main.focus();
    main.scrollIntoView({ block: 'start' });
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return;
    if (document.querySelector('dialog[open]')) return;
    if (event.target.matches('input, textarea, select, [contenteditable="true"]')) return;
    event.preventDefault();
    if (current !== 'ask') location.hash = '#/ask';
    // hash 变化是异步的；等视图切过去再把光标放进输入框
    setTimeout(focusComposer, 0);
  });

  on('lang', () => { applyStaticText(); syncTopbar(); setTitle(current); });
  on('learning', syncTopbar);
  on('connection', () => { syncTopbar(); checkHealth(); });
  on('reconnect', () => {
    state.identity = null;
    state.service = null;
    checkHealth();
    loadCatalog({ refresh: true });
    // 换了 Key 或身份：手里那份历史是上一个身份的。清掉，正开着的那一页自己重新取
    resetHistory();
    if (current === 'history') loadHistory();
    if (current === 'ask') showAsk();
  });

  checkHealth();
  loadCatalog();
}

boot();
window.__consoleBooted = true;
