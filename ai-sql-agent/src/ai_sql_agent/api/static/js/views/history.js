/* 历史查询：问过什么、什么时候问的、当时结果如何。

   历史来自服务端的审计记录，那里**不存结果集**——只有问题、SQL 与元数据。
   所以一条记录有两种可能，页面必须让人一眼分清：

   - 本次会话缓存：这个标签页里刚查过，结果还在内存里，可以回看；
   - 仅历史 SQL：只剩当时执行的 SQL，要看数据得重新执行。

   重新执行是一次新的查询，读的是现在的数据。旧结果和新结果永远分开显示、分开标注。 */

import { $, copyText, delegate, html, icon, setHtml, toast } from '../dom.js';
import { formatDateTime, formatDuration } from '../format.js';
import { history, loadHistory } from '../historyStore.js';
import { has, t } from '../i18n.js';
import { historyStatus } from '../model.js';
import { renderResult } from '../result.js';
import { on, resultCache, state } from '../state.js';
import { historyTone, notice, sourceLabel } from '../steps.js';
import { bindTurnActions, newTurnUi } from '../turnActions.js';
import { rerun } from './ask.js';

const FILTERS = ['all', 'success', 'no_data', 'review', 'failed', 'other'];

let root;
let selected = '';
let search = '';
let filter = 'all';
const cachedTurns = new Map();     // trace_id -> 回看缓存结果用的 turn（记着折叠、翻页状态）

function statusChip(status) {
  const [tone, glyph] = historyTone(status);
  return html`<span class="chip ${tone}">${icon(glyph, 'icon-sm')}${t('hstatus.' + status)}</span>`;
}

function matchesFilter(item, status) {
  if (filter === 'all') return true;
  // 「返回了数据」只收确实知道有数据的。老记录（只知道跑完了）、降级结果、对话回答
  // 归到「其他」——把「不知道」算成「有数据」，筛出来的就不是它说的那一类了。
  if (filter === 'review') return status === 'review' || status === 'partial';
  if (filter === 'other') return ['completed', 'degraded', 'conversation'].includes(status);
  return status === filter;
}

function visibleItems() {
  const needle = search.trim().toLowerCase();
  return history.items
    .map((item) => ({ item, status: historyStatus(item) }))
    .filter(({ item, status }) => matchesFilter(item, status)
      && (!needle || [item.query, item.trace_id, ...(item.databases || [])].join(' ').toLowerCase().includes(needle)));
}

function availability(item) {
  if (resultCache.has(item.trace_id)) {
    return html`<span class="chip chip-info">${t('history.cached')}</span>`;
  }
  return html`<span class="chip">${t((item.sqls || []).length ? 'history.sqlOnly' : 'history.noSql')}</span>`;
}

function listItem({ item, status }) {
  return html`<li><button type="button" class="history-item" data-action="select" data-trace="${item.trace_id}"
      aria-current="${item.trace_id === selected ? 'true' : 'false'}">
    <span class="history-q">${item.query}</span>
    <span class="history-meta">
      <span>${formatDateTime(item.created_at)}</span>
      ${statusChip(status)}
      ${availability(item)}
    </span>
  </button></li>`;
}

function cachedTurn(item) {
  const cached = resultCache.get(item.trace_id);
  if (!cached) return null;
  if (!cachedTurns.has(item.trace_id)) {
    cachedTurns.set(item.trace_id, {
      id: 'h' + item.trace_id.replace(/[^a-zA-Z0-9]/g, ''),
      question: item.query,
      result: cached.result,
      cached: true,
      status: 'done',
      ui: newTurnUi(),
    });
  }
  return cachedTurns.get(item.trace_id);
}

function detail() {
  const entry = history.items.find((item) => item.trace_id === selected);
  if (!entry) {
    return html`<div class="empty-state"><strong>${t('history.pick.title')}</strong>${t('history.pick.body')}</div>`;
  }
  const status = historyStatus(entry);
  const turn = cachedTurn(entry);
  const sqls = (entry.sqls || []).filter((item) => item && (item.sql || item.final_sql));
  const asked = formatDateTime(entry.created_at);
  // 实际查过的库看当时执行的 SQL；审计里的 databases 是检索考虑过的候选，两者不是一回事
  const queried = [...new Set(sqls.map((item) => item.database).filter(Boolean))];

  return html`<div class="stack-lg">
    <div class="stack">
      <h2 class="question-text" tabindex="-1" data-role="detail-title">${entry.query}</h2>
      <div class="row">${statusChip(status)}${availability(entry)}</div>
      <p class="muted">${t('history.statusHelp.' + status)}</p>
      <dl class="kv">
        <dt>${t('history.askedAt')}</dt><dd>${asked}</dd>
        ${queried.length ? html`<dt>${t('history.domains')}</dt>
          <dd>${queried.map(sourceLabel).join(', ')}</dd>` : ''}
      </dl>
    </div>

    <div class="card card-pad stack">
      <div><button type="button" class="btn btn-primary" data-action="rerun">${icon('refresh', 'icon-sm')}${t('history.rerun')}</button></div>
      <p class="muted">${t('history.rerunNote', { time: asked })}</p>
    </div>

    ${turn
      ? html`<section class="stack" aria-label="${t('history.cachedSection')}">
          <h3 class="section-title">${t('history.cachedSection')}</h3>
          <div data-turn="${turn.id}" data-trace="${entry.trace_id}">${renderResult(turn)}</div>
        </section>`
      : notice('info', 'info', t('history.sqlOnly.title'), html`<p>${t('history.sqlOnly.body')}</p>`)}

    ${entry.error ? notice('danger', 'error', t('history.error'), html`<p lang="zh">${entry.error}</p>`) : ''}

    ${!turn && sqls.length ? html`<section class="stack">
      <h3 class="section-title">${t('history.sqlSection')}</h3>
      <p class="muted">${t('history.sqlNote')}</p>
      ${sqls.map((item, index) => html`<div class="step-card">
        <div class="step-head"><span>${t('process.step', { n: index + 1 })}${item.database ? ` · ${sourceLabel(item.database)}` : ''}</span>
          <button type="button" class="btn btn-sm btn-ghost" data-action="copy-history-sql" data-index="${index}">
            ${icon('copy', 'icon-sm')}${t('action.copySql')}</button></div>
        <pre class="code">${item.sql || item.final_sql}</pre>
      </div>`)}
    </section>` : ''}

    <details class="disclosure"><summary>${t('history.technical')}</summary>
      <div class="disclosure-body"><dl class="kv">
        <dt>${t('process.traceId')}</dt><dd class="mono">${entry.trace_id}</dd>
        <dt>${t('process.route')}</dt><dd class="mono">${entry.route || '—'}</dd>
        <dt>${t('process.status')}</dt><dd class="mono">${entry.execution_status || t('history.statusUnknown')}</dd>
        ${(entry.databases || []).length ? html`<dt>${t('process.candidates')}</dt><dd>${entry.databases.map(sourceLabel).join(', ')}</dd>` : ''}
        ${(entry.tables || []).length ? html`<dt>${t('process.candidateTables')}</dt><dd class="mono">${entry.tables.join(', ')}</dd>` : ''}
        <dt>${t('process.time')}</dt><dd>${formatDuration(entry.duration_ms) || '—'}</dd>
        <dt>${t('process.tokens')}</dt><dd>${entry.total_tokens || '—'}</dd>
        ${entry.session_id ? html`<dt>${t('history.session')}</dt><dd class="mono">${entry.session_id}</dd>` : ''}
        ${entry.repaired_steps ? html`<dt>${t('history.repaired')}</dt><dd>${entry.repaired_steps}</dd>` : ''}
      </dl></div>
    </details>
  </div>`;
}

function body() {
  if (history.status === 'loading' || history.status === 'idle') {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('history.loading')}</div>`;
  }
  if (history.status === 'error') {
    const error = history.error || {};
    // 以别人的身份查询时，历史也是那个人的；服务端只让管理员看别人的历史
    const key = error.status === 403 && state.userId ? 'history.forbiddenActAs'
      : error.status === 0 ? 'error.network'
        : has('error.' + error.status) ? 'error.' + error.status
          : error.status >= 500 ? 'error.5xx' : 'error.generic';
    return html`${notice('danger', 'error', t('history.loadFailed'), html`<p>${t(key)}</p>
        <p class="original">${error.status ? `HTTP ${error.status}` : t('error.noResponse')}${error.serverMessage
          ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
      <div class="row"><button type="button" class="btn btn-sm" data-action="refresh">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button></div>`;
  }
  if (!history.items.length) {
    return html`<div class="empty-state"><strong>${t('history.empty.title')}</strong>${t('history.empty.body')}
      <div><a class="btn btn-sm" href="#/ask">${t('history.empty.action')}</a></div></div>`;
  }

  const items = visibleItems();
  return html`<div class="split">
    <div class="stack">
      <div class="filters">
        <div class="field field-wide"><label for="historySearch">${t('history.search')}</label>
          <input class="input" id="historySearch" type="search" value="${search}" autocomplete="off"
            placeholder="${t('history.searchPlaceholder')}"></div>
        <div class="field"><label for="historyFilter">${t('history.filter')}</label>
          <select class="select" id="historyFilter">${FILTERS.map((item) => html`<option value="${item}"
            ${item === filter ? 'selected' : ''}>${t('history.filter.' + item)}</option>`)}</select></div>
      </div>
      <p class="muted" role="status">${t('history.count', { n: items.length, total: history.items.length })}</p>
      <ul class="history-list" data-role="list">${items.length
        ? items.map(listItem)
        : html`<li class="empty-state">${t('history.noMatch')}</li>`}</ul>
    </div>
    <div class="detail-panel" data-role="detail">${detail()}</div>
  </div>`;
}

function draw() {
  const focusedSearch = document.activeElement && document.activeElement.id === 'historySearch';
  setHtml(root, html`<div class="page stack-lg">
    <div class="page-head">
      <div><h1 class="page-title" tabindex="-1" data-role="view-title">${t('history.title')}</h1>
        <p class="page-sub">${t('history.lead')}</p></div>
      <button type="button" class="btn btn-sm" data-action="refresh">${icon('refresh', 'icon-sm')}${t('action.refresh')}</button>
    </div>
    ${body()}
  </div>`);
  if (focusedSearch && $('historySearch')) {
    const input = $('historySearch');
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function drawList() {
  const list = root.querySelector('[data-role="list"]');
  if (!list) return draw();
  const items = visibleItems();
  setHtml(list, items.length ? items.map(listItem) : html`<li class="empty-state">${t('history.noMatch')}</li>`);
  const count = root.querySelector('.split [role="status"]');
  if (count) count.textContent = t('history.count', { n: items.length, total: history.items.length });
  return undefined;
}

function drawDetail() {
  const slot = root.querySelector('[data-role="detail"]');
  if (slot) setHtml(slot, detail());
}

export function showHistory() {
  draw();
  loadHistory();
}

export function initHistory() {
  root = $('view-history');

  root.addEventListener('input', (event) => {
    if (event.target.id !== 'historySearch') return;
    search = event.target.value;
    drawList();           // 只重画列表：输入框不动，光标不跳
  });
  root.addEventListener('change', (event) => {
    if (event.target.id !== 'historyFilter') return;
    filter = event.target.value;
    drawList();
  });

  const turnOf = (target) => {
    const holder = target.closest('[data-turn]');
    return holder ? cachedTurns.get(holder.dataset.trace) : null;
  };
  bindTurnActions(root, { turnOf, redraw: drawDetail });

  delegate(root, 'click', {
    refresh: () => loadHistory(),
    select: (target) => {
      selected = target.dataset.trace;
      drawList();
      drawDetail();
      const title = root.querySelector('[data-role="detail-title"]');
      // 窄屏下详情在列表下面：选中后把它带进视野并把焦点交过去
      if (title) { title.focus({ preventScroll: true }); title.scrollIntoView({ block: 'nearest' }); }
    },
    rerun: () => {
      const entry = history.items.find((item) => item.trace_id === selected);
      if (!entry) return;
      location.hash = '#/ask';
      rerun(entry.query, formatDateTime(entry.created_at));
    },
    'copy-history-sql': async (target) => {
      const entry = history.items.find((item) => item.trace_id === selected);
      const item = entry && (entry.sqls || []).filter((row) => row && (row.sql || row.final_sql))[Number(target.dataset.index)];
      if (item) toast(t(await copyText(item.sql || item.final_sql) ? 'toast.copied' : 'toast.copyFailed'));
    },
  });

  on('history', () => { if (!root.hidden) draw(); });
  on('lang', () => { if (!root.hidden) draw(); });
  on('learning', () => { if (!root.hidden) drawDetail(); });
  on('catalog', () => { if (!root.hidden) draw(); });
}
