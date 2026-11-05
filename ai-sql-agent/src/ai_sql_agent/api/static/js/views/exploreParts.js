/* 数据页各处共用的小件：库的标识色块、数据字典里的说明文字、记录数、目录没拿到时的提示。

   和整个数据页一样，这里没有任何表名、字段名或样例数据：服务端没给的，这里画不出来。 */

import { catalog, domains } from '../catalog.js';
import { html, icon } from '../dom.js';
import { descriptionOf, formatCount, labelOf } from '../format.js';
import { has, t } from '../i18n.js';
import { findProfile, knownRowCount, profileSupport, requestProfile } from '../profile.js';
import { notice } from '../steps.js';

// 一口气给多少张表各数一遍行数。超过这个数就不自动数了（每张表是一个请求、一遍全表扫描），
// 行数到那张表自己的页面上再看
const COUNT_UP_TO = 60;

/* 库的标识色：按目录里的顺序，前四个库各一个颜色（那四个色两两可辨，是校验过的一组），
   第五个起不上色——不为了凑数再生成颜色，色块里的缩写照样分得开。 */
export function databaseSlot(database) {
  const index = domains().findIndex((domain) => domain.database === database);
  return index >= 0 && index < 4 ? index + 1 : 0;
}

/* 色块里的缩写：库标识符第一段的前三个字母（契约规定库标识符形如「系统_db」）。 */
export function databaseCode(database) {
  const head = String(database || '').split(/[^A-Za-z0-9]+/).find(Boolean) || String(database || '?');
  return head.slice(0, 3).toUpperCase();
}

export const dbTile = (database, size = '') =>
  html`<span class="db-tile db-c${databaseSlot(database)} ${size}" aria-hidden="true">${databaseCode(database)}</span>`;

/* 说明文字。数据字典没有当前语言的版本时照原文给，并标明是原文。 */
export function description(entity) {
  const { text, original } = descriptionOf(entity);
  if (!text) return html`<span class="muted">${t('data.noDescription')}</span>`;
  return original
    ? html`<span class="original-note" lang="zh" title="${t('data.originalNote')}">${text}</span>`
    : html`<span>${text}</span>`;
}

/* 英文界面而数据字典只有中文说明时，先给一句英文概要，原文跟在后面。
   概要只是把目录里已有的名字列出来（这张表有哪些字段、这个域有哪些表），
   不是对原文的翻译——口径这种东西译错一个词，比让人看到一段中文危险。 */
function summarized(entity, key, names) {
  const { original } = descriptionOf(entity);
  if (!original || !names.length) return description(entity);
  return html`<span>${t(key, { names: names.join(', ') })}</span>${description(entity)}`;
}

export const tableSummary = (table) => summarized(
  table,
  'data.holds',
  (table.columns || [])
    .filter((column) => column.status === 'available')
    .map((column) => (column.unit ? `${labelOf(column)} (${column.unit})` : labelOf(column))),
);

export const domainSummary = (domain) => summarized(domain, 'data.contains', domain.tables.map(labelOf));

export const unavailableReason = (domain) => (has('data.reason.' + domain.status_reason)
  ? t('data.reason.' + domain.status_reason) : t('data.reason.unknown'));

export const fieldCount = (table) => (table.columns || []).length || table.column_count || 0;

export const allTables = () => domains().flatMap((domain) => domain.tables.map((table) => ({ domain, table })));

const countable = (domain, table) => domain.status === 'available'
  && table.status === 'available' && table.previewable;

/* 让行数取起来（已经取过的不会再取）。scope 是一个库名时只管那个库，不给就是全部。 */
export function requestCounts(scope = '') {
  if (profileSupport() !== 'on') return;
  const wanted = allTables().filter(({ domain, table }) =>
    countable(domain, table) && (!scope || domain.database === scope));
  if (wanted.length > COUNT_UP_TO) return;
  wanted.forEach(({ domain, table }) => requestProfile(domain.database, table.name, { breakdowns: false }));
}

/* 一个库的每张表都要带分布的概况（库页面上的卡片用）。 */
export function requestBreakdowns(domain) {
  if (profileSupport() !== 'on' || !domain) return;
  domain.tables.forEach((table) => {
    if (countable(domain, table)) requestProfile(domain.database, table.name);
  });
}

/* 一张表有多少条：数字，或者还不知道时是 null。 */
export const rowCount = (database, table) => knownRowCount(database, table);

/* 几张表的记录数加起来：{ total, known, failed, unasked, expected }。
   known 是数出来的，failed 是没数出来的，unasked 是根本没去数的（表太多时不自动数）。 */
export function sumCounts(pairs) {
  const sum = { total: 0, known: 0, failed: 0, unasked: 0, expected: 0 };
  pairs.forEach(({ domain, table }) => {
    if (!countable(domain, table)) return;
    sum.expected += 1;
    const count = knownRowCount(domain.database, table.name);
    if (count !== null) { sum.total += count; sum.known += 1; return; }
    const entry = findProfile(domain.database, table.name, { breakdowns: false });
    if (!entry) sum.unasked += 1;
    else if (entry.status === 'error') sum.failed += 1;
  });
  return sum;
}

/* 一张表的记录数。还在数时是一个占位；没去数、没数出来、服务不给概况时什么都不写。 */
export function countChip(database, table) {
  if (profileSupport() !== 'on') return '';
  const count = knownRowCount(database, table);
  if (count !== null) return html`<span class="dx-count">${formatCount(count)}</span>`;
  const entry = findProfile(database, table, { breakdowns: false });
  return entry && entry.status === 'loading'
    ? html`<span class="dx-count is-pending" aria-hidden="true">···</span>` : '';
}

/* 「就这份数据提问」带去问答页的话头；问答页显示出来之后取走。 */
let pendingPrefill = '';
export function setPrefill(text) { pendingPrefill = text; }
export function consumePrefill() {
  const text = pendingPrefill;
  pendingPrefill = '';
  return text;
}

/* ─── 目录还没拿到时的几种状态 ──────────────────────────────────── */

export function catalogProblem() {
  if (catalog.status === 'idle' || catalog.status === 'loading') {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('data.loading')}</div>`;
  }
  if (catalog.status === 'unsupported') {
    return notice('info', 'info', t('data.unsupported.title'), html`<p>${t('data.unsupported.body')}</p>`);
  }
  if (catalog.status === 'disabled') {
    return notice('info', 'info', t('data.disabled.title'), html`<p>${t('data.disabled.body')}</p>`);
  }
  if (catalog.status === 'error') {
    const error = catalog.error || {};
    const key = error.status === 0 ? 'error.network'
      : has('error.' + error.status) ? 'error.' + error.status
        : error.status >= 500 ? 'error.5xx' : 'error.generic';
    return html`${notice('danger', 'error', t('data.error.title'), html`<p>${t(key)}</p>
        <p class="original">${error.status ? `HTTP ${error.status}` : t('error.noResponse')}${error.serverMessage
          ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
      <div class="row"><button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
        <button type="button" class="btn btn-sm btn-ghost" data-action="open-settings">${t('action.checkSettings')}</button></div>`;
  }
  return '';
}
