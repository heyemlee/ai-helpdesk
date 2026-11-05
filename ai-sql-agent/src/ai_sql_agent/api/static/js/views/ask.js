/* 业务查数：问一句，看结论、数字和表格，接着追问。

   没问过的时候是首页：一个输入框，下面是「能问哪些数据」「可以这样问」「最近问过」——
   三样都来自服务端（目录、历史），页面自己不写任何一句业务问题（见 suggest.js）。
   问了之后是对话：问题在右、答案在左，输入框停在底部。

   一轮问答是一个 turn 对象；界面状态（哪个折叠打开、表格翻到第几页、看图还是看表、
   反馈草稿）都记在 turn.ui 上，重绘只是把 turn 再画一遍——所以切语言、切学习模式、
   回填耗时都不会把用户展开的东西收回去。 */

import { api } from '../api.js';
import { catalog, domainTitle, domains, ensureIdentity, findTable } from '../catalog.js';
import { dataHref } from '../dataRoute.js';
import { $, announce, delegate, html, icon, setHtml } from '../dom.js';
import { formatRelative, formatTime, labelOf } from '../format.js';
import { findHistory, history, loadHistory } from '../historyStore.js';
import { t } from '../i18n.js';
import { historyStatus, normalizeResult } from '../model.js';
import { renderLearning, renderStageTrack } from '../process.js';
import {
  announcement, pendingStage, renderPending, renderResult, renderServiceError, renderStopped,
} from '../result.js';
import { emit, newSessionId, on, resultCache, save, state, uid } from '../state.js';
import { historyTone, notice } from '../steps.js';
import { buildSuggestions, pageCount, pageOf } from '../suggest.js';
import { bindTurnActions, newTurnUi } from '../turnActions.js';
import { databaseSlot, dbTile } from './exploreParts.js';

const MAX_INPUT_HEIGHT = 180;
const RECENT_MAX = 4;
const RELATIVE_REFRESH_MS = 60000;   // 「5 分钟前」这类字样多久重算一次
// 发送键在发出问题后原地变成「停止」。连点两下的第二下不该把刚发出去的问题停掉
const STOP_GUARD_MS = 600;

const turns = [];
let busy = null;              // 正在等结果的那一轮
let streamUnsupported = false;
let suggestScope = '';        // 建议问题只看哪个库的；空着 = 全部
let suggestPage = 0;          // 「换一批」翻到了第几页
let root;

const newTurn = (question, extra = {}) => ({
  id: 't' + uid().slice(0, 10),
  question,
  askedAt: new Date().toISOString(),
  status: 'pending',          // pending | done | error | stopped
  result: null,
  error: null,
  transport: 'query',
  stages: [],
  stageNow: null,
  startedAt: performance.now(),
  controller: null,
  rerunOf: null,
  cached: false,
  ui: { ...newTurnUi(), phase: '' },   // phase：上一次画出来时的状态，进场动效据此只放一次
  ...extra,
});

function chooseTransport() {
  // 长期记忆只有 /query 支持；服务端没说支持流式（或连目录都没拿到）就不走流
  const streaming = Boolean(state.service && state.service.streaming) && !streamUnsupported;
  if (state.longTerm || !streaming || state.transport === 'query') return 'query';
  if (state.transport === 'stream') return 'a2a-stream';
  return state.learning ? 'a2a-stream' : 'query';
}

/* ─── 输入框 ───────────────────────────────────────────────────── */

/* 发送键。等结果的时候它是「停止」：一个点不了的灰色按钮什么也没告诉人。 */
function sendButton() {
  if (busy) {
    return html`<button class="send-btn is-stop" id="askSend" type="button" data-action="stop-current"
      aria-label="${t('action.stop')}" title="${t('action.stop')}">${icon('stop')}</button>`;
  }
  return html`<button class="send-btn" id="askSend" type="submit" aria-label="${t('action.send')}"
    title="${t('action.send')}">${icon('send')}</button>`;
}

function composer(docked) {
  // 「重要的数字先核对」那句提醒跟着答案走：对话里一直在输入框下面，首页上没有答案，不放
  return html`<form class="composer${docked ? ' is-dock' : ''}" data-form="ask">
    <label class="sr-only" for="askInput">${t('ask.inputLabel')}</label>
    <textarea id="askInput" rows="${docked ? 1 : 2}" autocomplete="off"
      placeholder="${t(docked ? 'ask.followUpPlaceholder' : 'ask.placeholder')}"></textarea>
    <div class="composer-bar">
      ${docked
        ? html`<button type="button" class="act" data-action="new" title="${t('ask.newHint')}">${icon('plus', 'icon-sm')}${t('ask.new')}</button>`
        : html`<span class="composer-meta" title="${t('ask.readOnlyHint')}">${icon('lock', 'icon-sm')}${t('ask.readOnly')}</span>`}
      <span class="composer-keys" aria-hidden="true">${t('ask.keys')}</span>
      <span class="composer-send" data-role="send-slot" data-mode="${busy ? 'stop' : 'send'}">${sendButton()}</span>
    </div>
  </form>
  ${docked ? html`<p class="composer-caution">${t('ask.caution')}</p>` : ''}`;
}

function syncComposer() {
  const input = $('askInput');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, MAX_INPUT_HEIGHT) + 'px';
  // 没到高度上限时不留滚动条：自动长高的输入框旁边多一截滚动条只会让人以为还有内容
  input.style.overflowY = input.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden';

  const slot = root.querySelector('[data-role="send-slot"]');
  const mode = busy ? 'stop' : 'send';
  if (slot && slot.dataset.mode !== mode) {
    slot.dataset.mode = mode;
    setHtml(slot, sendButton());
  }
  const send = $('askSend');
  if (send && !busy) send.disabled = !input.value.trim();
  const fresh = root.querySelector('[data-action="new"]');
  if (fresh) fresh.disabled = Boolean(busy);
}

/* ─── 首页 ─────────────────────────────────────────────────────── */

const catalogPending = () => catalog.status === 'idle' || catalog.status === 'loading';

/* 服务连不上时在输入框下面说一声。顶栏右上角那个小圆点太容易看漏，
   而这时候发问只会等来一条「请求没成功」。 */
function offline() {
  if (state.live !== false) return '';
  return html`<div class="ask-offline">${notice('danger', 'error', t('conn.down'), html`<p>${t('error.network')}</p>
    <p><button type="button" class="link-btn" data-action="open-settings">${t('action.checkSettings')}</button></p>`)}</div>`;
}

/* 当前身份看得到、此刻读得到的业务域：能问的就是这些。 */
const askable = () => (catalog.status === 'ready' ? domains().filter((domain) => domain.status === 'available') : []);

/* 正在显示的那一组建议：全部，或者只看某一个库的。everything 是已经拼好的全部建议，不给就现拼。 */
function suggestionList(everything = catalog.status === 'ready' ? buildSuggestions(domains()) : []) {
  return suggestScope ? everything.filter((item) => item.database === suggestScope) : everything;
}

/* 可以这样问。上面一排是能问的业务域（点一个只看它的问法），下面是拼出来的问题。
   目录还没到时先占着位置，到了再换成真的——免得下面的内容跳一下。 */
function suggestions() {
  if (catalogPending()) {
    return html`<div class="ask-section" aria-hidden="true">
      <div class="ask-section-head"><span class="ask-section-title">&nbsp;</span></div>
      <div class="scope-row"><span class="scope-chip is-ghost"></span><span class="scope-chip is-ghost"></span><span class="scope-chip is-ghost"></span></div>
      <div class="suggest-grid">${[0, 1, 2, 3].map(() => html`<span class="suggest is-ghost"></span>`)}</div>
    </div>`;
  }
  const available = askable();
  // 换了身份或目录之后，之前选的那个库可能已经不在了
  if (suggestScope && !available.some((domain) => domain.database === suggestScope)) suggestScope = '';
  const everything = catalog.status === 'ready' ? buildSuggestions(domains()) : [];
  if (!everything.length) {
    return html`<p class="ask-fallback">${t('ask.noSuggest')} <a href="#/data">${t('ask.browseData')}</a></p>`;
  }
  const list = suggestionList(everything);
  return html`<section class="ask-section" aria-labelledby="suggestTitle">
    <div class="ask-section-head">
      <h2 class="ask-section-title" id="suggestTitle">${t('ask.suggestTitle')}</h2>
      ${pageCount(list) > 1 ? html`<button type="button" class="act" data-action="shuffle">${icon('refresh', 'icon-sm')}${t('ask.shuffle')}</button>` : ''}
    </div>
    ${available.length > 1 ? html`<div class="scope-row" role="group" aria-label="${t('ask.scope')}">
      <button type="button" class="scope-chip is-all" data-action="scope" data-name="" aria-pressed="${String(!suggestScope)}">${t('ask.scopeAll')}</button>
      ${available.map((domain) => html`<button type="button" class="scope-chip" data-action="scope" data-name="${domain.database}"
        aria-pressed="${String(suggestScope === domain.database)}">${dbTile(domain.database, 'is-xs')}<span>${domainTitle(domain)}</span></button>`)}
    </div>` : ''}
    ${list.length ? html`<div class="suggest-grid">${pageOf(list, suggestPage).map((item, index) => {
      const table = findTable(item.database, item.table);
      return html`<button type="button" class="suggest db-c${databaseSlot(item.database)}" data-action="suggest" data-index="${index}">
        <span class="suggest-src">${dbTile(item.database, 'is-xs')}<span>${table ? labelOf(table) : item.table}</span></span>
        <span class="suggest-text">${item.text}</span>
        <span class="suggest-go" aria-hidden="true">${icon('arrowUpRight', 'icon-sm')}</span>
      </button>`;
    })}</div>` : html`<p class="ask-empty">${t('ask.scopeEmpty')}</p>`}
    <p class="ask-note">${t('ask.suggestNote')} <a href="${suggestScope ? dataHref(suggestScope) : '#/data'}">${t('ask.browseData')}</a></p>
  </section>`;
}

/* 最近问过的几个问题（同一句话只留最近的一次）。
   认的是「这个身份的历史取成过」，不是「此刻状态正常」：之后某一次刷新失败了，手里那份照样是
   真的，不让整段跟着消失。换了身份时这份会被清空（见 historyStore 的 resetHistory）。 */
function recentItems() {
  if (!history.loadedAt) return [];
  const seen = new Set();
  const items = [];
  for (const item of history.items) {
    const text = String(item.query || '').trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    items.push(item);
    if (items.length === RECENT_MAX) break;
  }
  return items;
}

function recent() {
  const items = recentItems();
  if (!items.length) return '';
  return html`<section class="ask-section" aria-labelledby="recentTitle">
    <div class="ask-section-head">
      <h2 class="ask-section-title" id="recentTitle">${t('ask.recentTitle')}</h2>
      <a class="act" href="#/history">${t('ask.recentAll')}${icon('chevronRight', 'icon-sm')}</a>
    </div>
    <ul class="recent-list">${items.map((item, index) => {
      const status = historyStatus(item);
      const [tone, glyph] = historyTone(status);
      return html`<li><button type="button" class="recent-item" data-action="recent" data-index="${index}">
        <span class="recent-status tone-${tone.replace('chip-', '') || 'plain'}" title="${t('hstatus.' + status)}">${icon(glyph, 'icon-sm')}<span
          class="sr-only">${t('hstatus.' + status)}</span></span>
        <span class="recent-q">${item.query}</span>
        <span class="recent-time" data-at="${item.created_at}">${formatRelative(item.created_at)}</span>
      </button></li>`;
    })}</ul>
  </section>`;
}

function home() {
  return html`<div class="ask-home">
    <div class="ask-hero">
      <span class="ask-mark" aria-hidden="true">${icon('logo')}</span>
      <h1 tabindex="-1" data-role="view-title">${t('ask.title')}</h1>
      <p class="lead">${t('ask.lead')}</p>
    </div>
    ${composer(false)}
    <div data-role="offline">${offline()}</div>
    <div data-role="suggest">${suggestions()}</div>
    <div data-role="recent">${recent()}</div>
  </div>`;
}

/* 首页上的某一块单独重画（目录、历史各自回来的时候），不动输入框。 */
function drawSlot(name, content) {
  const slot = root.querySelector(`.ask-home [data-role="${name}"]`);
  if (slot) setHtml(slot, content());
}

/* ─── 对话 ─────────────────────────────────────────────────────── */

function conversation() {
  return html`<div class="conversation">
    <div class="turns"><div class="turns-inner">
      <h1 class="sr-only" tabindex="-1" data-role="view-title">${t('ask.conversation')}</h1>
      ${turns.map((turn) => html`<article class="turn" id="${turn.id}" data-turn="${turn.id}">
        <div class="turn-q" data-role="question"></div>
        <div class="turn-a" data-role="answer"></div>
      </article>`)}
    </div></div>
    <div class="composer-dock"><div class="composer-dock-inner">${composer(true)}</div></div>
  </div>`;
}

function shell() {
  const previous = $('askInput');
  const draft = previous ? previous.value : '';
  // 用键盘回车发的问题，发完光标留在输入框；点按钮发的（多半是手机）不抢焦点，免得键盘弹回来
  const refocus = Boolean(previous) && document.activeElement === previous;
  setHtml(root, turns.length ? conversation() : home());
  turns.forEach(drawTurn);
  if (draft) $('askInput').value = draft;
  if (refocus) $('askInput').focus();
  syncComposer();
}

/* 进场动效：去掉类名、强制回流、再加回去，动画才会从头放一遍。 */
function replay(element) {
  element.classList.remove('is-entering');
  void element.offsetWidth;
  element.classList.add('is-entering');
}

function drawTurn(turn) {
  const element = $(turn.id);
  if (!element) return;
  const question = element.querySelector('[data-role="question"]');
  const answer = element.querySelector('[data-role="answer"]');
  const elapsed = Math.floor((performance.now() - turn.startedAt) / 1000);
  let body;
  if (turn.status === 'pending') body = renderPending(turn, elapsed);
  else if (turn.status === 'stopped') body = renderStopped(turn);
  else if (turn.status === 'error') body = renderServiceError(turn);
  else body = renderResult(turn);

  setHtml(question, html`<div class="q-bubble"><h2 class="question-text">${turn.question}</h2></div>
    <div class="q-meta">
      <span>${t(turn.rerunOf ? 'ask.askedRerun' : 'ask.asked', { time: formatTime(turn.askedAt) })}</span>
      <button type="button" class="icon-btn icon-btn-sm" data-action="copy-question" aria-label="${t('action.copyQuestion')}"
        title="${t('action.copyQuestion')}">${icon('copy', 'icon-sm')}</button>
    </div>`);
  setHtml(answer, body);

  // 只在这一轮第一次画出来、以及它的状态变了（等待 → 有结果）时各放一次；
  // 排序、翻页、回填耗时引起的重画不带动效，否则每点一下表头整块答案都闪一下
  if (!turn.ui.phase) replay(question);
  if (turn.ui.phase !== turn.status) replay(answer);
  turn.ui.phase = turn.status;
}

/* 等待中来了一条阶段事件：只改那句状态、进度条和学习模式的时间线，不整块重画——
   整块重画会把正在转的动效一次次打回起点。 */
function drawStage(turn) {
  const element = $(turn.id);
  const text = element && element.querySelector('[data-role="stage-text"]');
  if (!text) { drawTurn(turn); return; }
  text.textContent = pendingStage(turn);
  const track = element.querySelector('[data-role="stage-track"]');
  if (track) setHtml(track, renderStageTrack(turn.stages));
  const learn = element.querySelector('[data-role="learn-slot"]');
  if (learn) setHtml(learn, renderLearning(turn));
}

/* ─── 提问 ─────────────────────────────────────────────────────── */

async function ask(question, extra = {}) {
  const text = String(question || '').trim();
  if (!text || busy) return;

  const turn = newTurn(text, extra);
  turn.controller = new AbortController();
  turns.push(turn);
  busy = turn;
  shell();
  const element = $(turn.id);
  if (element) element.scrollIntoView({ block: 'start' });

  // 发问之前先确认身份与服务能力（通常已经有了；页面刚打开、或者刚在设置里换过连接时
  // 要等几十毫秒）。没有它，user_id 只能填占位值，绑定了用户的 Key 会因此被拒；
  // 也不知道该不该走流式。目录拿不到不影响这一步——身份另有来源。
  await ensureIdentity();
  if (turn.controller.signal.aborted) {
    turn.status = 'stopped';
    turn.controller = null;
    busy = null;
    drawTurn(turn);
    syncComposer();
    return;
  }
  turn.transport = chooseTransport();
  // 走流式才需要重画（换成分段进度）；走 /query 的话等待态已经是对的
  if (turn.transport === 'a2a-stream') drawTurn(turn);

  const timer = setInterval(() => {
    const slot = $(turn.id) && $(turn.id).querySelector('[data-role="elapsed"]');
    const seconds = Math.floor((performance.now() - turn.startedAt) / 1000);
    if (slot && seconds >= 2) slot.textContent = t('ask.elapsed', { n: seconds });
  }, 1000);

  try {
    let payload;
    if (turn.transport === 'a2a-stream') {
      try {
        const streamed = await api.stream(text, {
          signal: turn.controller.signal,
          onStage: (stage) => { turn.stages.push(stage); turn.stageNow = stage; drawStage(turn); },
          onTask: (runId) => { turn.runId = runId; },
        });
        payload = streamed.payload;
      } catch (error) {
        // 服务端明确说流式没开（-32004）：这是「换条路」，不是失败。
        // 其余错误不重试——流已经打开过的话，重发等于让同一个问题再跑一遍。
        if (error.code !== 'stream_unsupported') throw error;
        streamUnsupported = true;
        turn.transport = 'query';
        turn.stages = [];
        turn.stageNow = null;
        drawTurn(turn);
        payload = await api.query(text, turn.controller.signal);
      }
    } else {
      payload = await api.query(text, turn.controller.signal);
    }

    turn.result = normalizeResult(payload, {
      transport: turn.transport,
      stages: turn.stages,
      clientMs: performance.now() - turn.startedAt,
    });
    turn.status = 'done';
    if (turn.result.traceId) {
      resultCache.set(turn.result.traceId, {
        result: turn.result, question: text, capturedAt: turn.result.receivedAt,
      });
    }
    state.live = true;
  } catch (error) {
    if (error && error.name === 'AbortError') {
      turn.status = 'stopped';
    } else {
      turn.status = 'error';
      turn.error = error;
      if (!error.status) { state.live = false; emit('connection'); }
    }
  } finally {
    clearInterval(timer);
    turn.controller = null;
    busy = null;
    drawTurn(turn);
    syncComposer();
    revealAnswer(turn);
    announce(announcement(turn));
  }

  if (turn.status === 'done') backfill(turn);
  else if (turn.status === 'stopped') loadHistory();
}

/* 停止：先请服务端停下这次运行，再断开连接。服务端默认在连接断开后也会停下
   （AI_SQL_CANCEL_ON_DISCONNECT），显式取消不依赖这个开关，还能知道服务端那边的结果：
   受理了、已经跑完（409），还是请求失败（失败不影响本地的停止）。停止提示按这个结果写。
   运行号只有流式请求在首帧里拿得到。 */
function stopTurn(turn) {
  if (!turn || !turn.controller) return;
  if (turn.runId) {
    turn.cancel = 'sent';
    api.cancelRun(turn.runId)
      .then(() => { turn.cancel = 'accepted'; })
      .catch((error) => { turn.cancel = error && error.status === 409 ? 'finished' : 'failed'; })
      .finally(() => { if (turn.status === 'stopped') drawTurn(turn); });
  }
  turn.controller.abort();
}

/* 答案回来时把这一轮顶到视口上方。等待态只有几行，答案比它长得多，
   不动的话结论常常落在底部输入框后面。用户已经滚到别处去看东西了就不打扰。 */
function revealAnswer(turn) {
  const element = $(turn.id);
  if (!element) return;
  const box = element.getBoundingClientRect();
  if (box.bottom <= 0 || box.top >= window.innerHeight) return;
  // 直接跳过去，不做平滑滚动：紧接着还有一次回填重画，动画会被它打断，停在半路
  element.scrollIntoView({ block: 'start' });
}

/* 服务端记账的耗时、token 与检索命中的表不在 /query 的响应里，查完从 /history 取回。 */
async function backfill(turn) {
  await loadHistory();
  const entry = findHistory(turn.result.traceId);
  if (!entry) return;
  turn.result.serverMs = typeof entry.duration_ms === 'number' ? entry.duration_ms : null;
  turn.result.totalTokens = typeof entry.total_tokens === 'number' ? entry.total_tokens : null;
  turn.result.candidateTables = Array.isArray(entry.tables) ? entry.tables : [];
  // 查询过程即使收着也重画：它的内容在折叠里已经画好了，不重画的话展开看到的是回填之前的那份
  drawTurn(turn);
}

/* ─── 对外 ─────────────────────────────────────────────────────── */

/* 历史页的「重新执行」：一次新的查询，新的一轮上标明它是重跑以及原记录的时间。

   必须换一个新会话。沿用当前会话的话，服务端的路由会发现「这个问题刚才问过、
   结果还在上下文里」，直接根据记忆作答而不去查库——那样「重新执行」拿回来的
   就是旧结果的复述，而页面却在说这是刚读出来的数据。 */
export function rerun(question, originalTime) {
  if (busy) return undefined;
  turns.length = 0;
  state.sessionId = newSessionId();
  save();
  return ask(question, { rerunOf: originalTime || '' });
}

/* 把一句话放进输入框，光标停在末尾：建议问题、最近问过、数据页的「就这张表提问」都走这里。
   只填不发——问什么、怎么改，由人定。 */
export function prefill(text) {
  const input = $('askInput');
  if (!input) return;
  input.value = text;
  syncComposer();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

export function focusComposer() {
  const input = $('askInput');
  if (input) input.focus();
}

/* 切到提问页时调（刚打开页面、从别的页回来、在设置里换了连接之后）。只管首页：

   - 「最近问过」要用历史。还没取过、或者上一次没取成，就取一次；取成过的不重复取，
     每问完一轮它会自己刷新。
   - 建议和「多久以前」可能已经过期：在数据页上逛的时候目录可能多取到了几张表的字段，
     时间也过去了一会儿。趁回来的时候重画一遍。 */
export function showAsk() {
  if (turns.length) return;
  if (history.status === 'idle' || history.status === 'error') loadHistory();
  drawSlot('suggest', suggestions);
  drawSlot('recent', recent);
}

const turnOf = (target) => {
  const holder = target.closest('[data-turn]');
  return holder ? turns.find((turn) => turn.id === holder.dataset.turn) : null;
};

export function initAsk() {
  root = $('view-ask');

  root.addEventListener('submit', (event) => {
    if (!event.target.matches('[data-form="ask"]')) return;
    event.preventDefault();
    const input = $('askInput');
    const text = input.value;
    if (!text.trim() || busy) return;
    input.value = '';
    ask(text);
  });

  root.addEventListener('input', (event) => {
    if (event.target.id === 'askInput') syncComposer();
  });

  root.addEventListener('keydown', (event) => {
    if (event.target.id !== 'askInput') return;
    // 输入法正在组字时的按键不算（回车是在选字，方向键是在挑候选）
    if (event.isComposing || event.keyCode === 229) return;
    // 输入框空着时按 ↑：把上一个问题取回来，改一改再发
    if (event.key === 'ArrowUp' && !event.target.value && turns.length) {
      event.preventDefault();
      prefill(turns[turns.length - 1].question);
      return;
    }
    // Enter 发送，Shift+Enter 换行
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    event.target.form.requestSubmit();
  });

  bindTurnActions(root, { turnOf, redraw: drawTurn });

  delegate(root, 'click', {
    // 填进去的就是卡片上写着的那句话。不拿序号去重新拼出来的清单里找：目录在这期间可能
    // 多取到了几张表的字段，清单变了，序号对上的就成了另一句
    suggest: (target) => {
      const text = target.querySelector('.suggest-text');
      if (text) prefill(text.textContent);
    },
    scope: (target) => {
      suggestScope = target.dataset.name || '';
      suggestPage = 0;
      drawSlot('suggest', suggestions);
    },
    shuffle: () => {
      const pages = pageCount(suggestionList());
      suggestPage = (suggestPage + 1) % pages;
      drawSlot('suggest', suggestions);
      announce(`${t('ask.suggestTitle')} ${suggestPage + 1} / ${pages}`);
    },
    recent: (target) => {
      const text = target.querySelector('.recent-q');
      if (text) prefill(text.textContent.trim());
    },
    new: () => {
      if (busy) return;
      turns.length = 0;
      state.sessionId = newSessionId();
      save();
      shell();
      focusComposer();
    },
    stop: (target) => stopTurn(turnOf(target)),
    'stop-current': () => {
      if (busy && busy.controller && performance.now() - busy.startedAt > STOP_GUARD_MS) stopTurn(busy);
    },
    'ask-again': (target) => {
      const turn = turnOf(target);
      if (turn) ask(turn.question);
    },
    'follow-up': () => focusComposer(),
    'open-settings': () => emit('open-settings'),
  });

  on('lang', shell);
  on('learning', () => turns.forEach(drawTurn));
  // 换了服务地址或 Key：上一台服务说过「不支持流式」，不代表这一台也不支持
  on('reconnect', () => { streamUnsupported = false; });
  on('catalog', () => {
    if (!turns.length) {
      // 目录变了，建议跟着重拼。页码不用动：翻过了头会自己绕回第一页（见 pageOf）
      drawSlot('suggest', suggestions);
    } else {
      turns.forEach((turn) => { if (turn.status === 'done') drawTurn(turn); });
    }
  });
  on('history', () => drawSlot('recent', recent));
  on('health', () => drawSlot('offline', offline));

  // 页面一直开着的话，「刚刚」不能一直是「刚刚」。只改那几个字，不重画列表——
  // 整段重画赶上鼠标正按在某一行上，那一下点击就丢了
  setInterval(() => {
    root.querySelectorAll('.recent-time[data-at]').forEach((element) => {
      element.textContent = formatRelative(element.dataset.at);
    });
  }, RELATIVE_REFRESH_MS);

  shell();
}
