/* 一张表的工作区：先看整体（多少条、状态怎么分布、落在哪些月份），再看记录，
   点开一条记录还能看到别的库里和它对得上的记录。

   从上到下是一条线：搜索与筛选 → 概览 → 记录。筛选条件管它下面的一切——
   概览里的数字和表格里的行说的永远是同一批记录。概览里的每个取值、每根柱子都能点，
   点一下就是加一个筛选条件，不用自己去拼「字段 / 条件 / 值」。

   数据全部来自服务端：记录来自 /catalog/{库}/{表}/rows，统计来自 …/profile，
   两者都按当前身份算。这个文件里没有任何表名、字段名或样例数据。 */

import { api } from '../api.js';
import { catalog, domainTitle, findDomain, findTable } from '../catalog.js';
import { columnChart, meter, rebucket, seriesSlots, stackedBar, valueList } from '../charts.js';
import { PAGE_SIZES, dataHref, formatView, freshView, parseView } from '../dataRoute.js';
import { $, announce, copyText, html, icon, setHtml, toast } from '../dom.js';
import {
  descriptionOf, formatCell, formatCompact, formatCount, formatNumber, formatRange, formatShare,
  formatTime, labelOf, localized, toTsv,
} from '../format.js';
import { has, t, tn } from '../i18n.js';
import { buildLinks, relatedTables } from '../links.js';
import { peekProfile, profileSupport, requestProfile, retryProfile } from '../profile.js';
import { save, state } from '../state.js';
import { notice } from '../steps.js';
import { renderTable, valuePill } from '../table.js';
import {
  catalogProblem, dbTile, fieldCount, setPrefill, tableSummary, unavailableReason,
} from './exploreParts.js';

const OPERATORS = {
  text: ['contains', 'eq', 'ne', 'starts_with', 'is_empty', 'not_empty'],
  number: ['eq', 'ne', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'],
  date: ['eq', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'],
  binary: ['is_empty', 'not_empty'],     // 二进制字段只能问「有没有内容」
};
const VALUELESS = ['is_empty', 'not_empty'];
const SEARCH_DELAY_MS = 350;
const TOP_VALUES = 6;          // 取值很多的字段，清单里先列最多的这几个
const RELATED_ROWS = 5;        // 记录详情里，每张相关的表先看几条
const RELATED_COLUMNS = 5;     // …每条看几个字段
const MAX_RELATED = 8;         // 一条记录最多同时去几张表里找
const RELATED_TTL_MS = 60000;  // 同一个键值刚查过就不再查：来回看两条记录不该把浏览的限流额度用掉
const RELATED_KEEP = 200;
const STEP_DELAY_MS = 350;     // 用「下一条」快速翻的时候，停下来才去查相关记录
// 相关表里先看哪类字段：状态与类别最先，其次是名称编号这类标识，再是日期、数字，备注最后
const BRIEF_RANK = {
  filter: 0, dimension: 0, output_dimension: 0, join_key: 1, identifier: 1, time: 2, metric: 3, metric_filter: 3,
};

let slot;                      // 页面内容所在的容器（由 explore.js 给）
let route = { database: '', table: '' };
let view = freshView();
let draft = { column: '', op: '' };
let panel = '';                // 工具栏下面展开的是哪一块：'' | 'filter' | 'columns'
let hidden = new Set();        // 手动收起的字段
let showEmpty = false;         // 一条内容都没有的字段默认收起；这是「全部显示」
let expanded = false;          // 概览是不是展开到全部卡片
let preview = { status: 'idle', data: null, error: null };
let lastProfile = null;        // 上一份画出来的概况：新的还在算时先留着它，不闪
let controller = null;
let profileAbort = null;
let sequence = 0;
let detail = null;             // 打开着的那条记录：{ index, related: Map }
let detailAbort = null;
let detailTimer = null;
let searchTimer = null;
const relatedCache = new Map();   // 键 → { at, data }：相关记录的短缓存，跟着目录作废
let relatedFor = null;            // 上面这份缓存是对着哪一份目录取的

const operatorsFor = (column) => OPERATORS[column && OPERATORS[column.kind] ? column.kind : 'text'];
const serverOperators = () => (state.service && state.service.preview && state.service.preview.operators) || null;
const maxFilters = () => (state.service && state.service.preview && state.service.preview.max_filters) || 5;
const insightsOn = () => !(state.explorer && state.explorer.insights === false);

const currentDomain = () => findDomain(route.database);
const currentTable = () => findTable(route.database, route.table);
const columnEntry = (name) => ((currentTable() || {}).columns || []).find((column) => column.name === name) || null;
const columnLabel = (name) => { const entry = columnEntry(name); return entry ? labelOf(entry) : name; };
const isFiltered = () => Boolean(view.search || view.filters.length);
const usable = (table, domain) => table && domain && table.status === 'available'
  && domain.status === 'available' && table.previewable;

/* ─── 概况 ─────────────────────────────────────────────────────── */

const baseEntry = () => requestProfile(route.database, route.table);
const baseData = () => { const entry = baseEntry(); return entry && entry.status === 'ready' ? entry.data : null; };

/* 当前这组条件下的概况。没有条件时就是整张表的那一份。 */
function viewEntry() {
  if (!isFiltered()) return baseEntry();
  return requestProfile(route.database, route.table, {
    search: view.search, filters: view.filters, signal: profileAbort ? profileAbort.signal : null,
  });
}

function refreshProfiles() {
  if (profileSupport() !== 'on' || !usable(currentTable(), currentDomain())) return;
  if (profileAbort) profileAbort.abort();
  profileAbort = new AbortController();
  baseEntry();
  viewEntry();
}

/* 这个字段的取值各是第几个颜色。按整张表的取值清单定，筛选之后不变。 */
function slotsFor(name, profile = baseData()) {
  const facet = profile && profile.facets.find((item) => item.column === name);
  return seriesSlots(facet);
}

/* 整张表里一条内容都没有的字段。 */
function emptyColumns() {
  const profile = baseData();
  if (!profile || !profile.total_rows) return [];
  return profile.columns.filter((column) => column.filled === 0).map((column) => column.name);
}

/* ─── 筛选条件 ─────────────────────────────────────────────────── */

const sameValue = (left, right) => String(left).toLowerCase() === String(right).toLowerCase();

/* 时间轴点出来的是一对条件（不早于 start、早于 end）。认出这样的一对，当成一个范围来显示。 */
function dateRange(name) {
  const own = view.filters.filter((filter) => filter.column === name);
  const from = own.find((filter) => filter.op === 'gte');
  const until = own.find((filter) => filter.op === 'lt');
  return own.length === 2 && from && until ? { start: String(from.value), end: String(until.value) } : null;
}

/* [start, end) 正好是一个月、一个季度或一年时，写成人读的样子；否则写起止日期。 */
function periodLabel(range) {
  const match = /^(\d{4})-(\d{2})-01$/;
  const [a, b] = [match.exec(range.start), match.exec(range.end)];
  if (a && b) {
    const months = (Number(b[1]) - Number(a[1])) * 12 + Number(b[2]) - Number(a[2]);
    const month = Number(a[2]);
    if (months === 1) return `${a[1]}-${a[2]}`;
    if (months === 3 && month % 3 === 1) return `${a[1]} Q${(month + 2) / 3}`;
    if (months === 12 && month === 1) return a[1];
  }
  return t('filter.range', { from: range.start, to: range.end });
}

function filterLabel(filter) {
  const name = columnLabel(filter.column);
  return VALUELESS.includes(filter.op)
    ? `${name} ${t('filter.op.' + filter.op)}`
    : `${name} ${t('filter.op.' + filter.op)} ${filter.value}`;
}

/* 要显示的筛选标签：[{ label, columns?, index? }]。一对日期条件合成一个。 */
function chipItems() {
  const items = [];
  const ranged = new Set();
  view.filters.forEach((filter, index) => {
    if (ranged.has(filter.column)) return;
    const range = dateRange(filter.column);
    if (range) {
      ranged.add(filter.column);
      items.push({ label: `${columnLabel(filter.column)}: ${periodLabel(range)}`, column: filter.column });
    } else {
      items.push({ label: filterLabel(filter), index });
    }
  });
  return items;
}

/* 换掉某个字段上的全部条件。放不下（超过上限）时不改，告诉用户。 */
function replaceFilters(name, next) {
  const rest = view.filters.filter((filter) => filter.column !== name);
  if (rest.length + next.length > maxFilters()) {
    toast(t('filter.tooMany', { n: maxFilters() }));
    return;
  }
  applyView({ filters: [...rest, ...next], page: 1 });
}

/* ─── 页面骨架 ─────────────────────────────────────────────────── */

function relatedChips(table) {
  const links = buildLinks(catalog.data.domains);
  const seen = new Map();
  relatedTables(links, route.database, table.name).forEach((item) => {
    item.others.forEach((other) => {
      const key = other.database + '\u0000' + other.table;
      if (!seen.has(key)) seen.set(key, other);
    });
  });
  if (!seen.size) return '';
  return html`<p class="rel-chips">${icon('link', 'icon-sm')}<span class="muted">${t('data.relatedTables')}</span>
    ${[...seen.values()].map((other) => html`<a class="chip chip-link" href="${dataHref(other.database, other.table)}">${dbTile(other.database, 'is-xs')}${labelOf(other.tableEntry)}</a>`)}</p>`;
}

function headMeta(table) {
  const profile = baseData();
  const parts = [];
  if (profile && typeof profile.total_rows === 'number') parts.push(tn('data.recordCount', profile.total_rows, { count: formatCount(profile.total_rows) }));
  parts.push(tn('data.fieldCount', fieldCount(table)));
  return parts.join(' · ');
}

function drawHeadMeta() {
  const node = slot && slot.querySelector('[data-role="headmeta"]');
  const table = currentTable();
  if (node && table) node.textContent = headMeta(table);
}

export function drawTable() {
  if (!slot) return;
  const problem = catalogProblem();
  const domain = currentDomain();
  const table = currentTable();

  if (problem || !catalog.data) {
    setHtml(slot, html`<div class="page stack-lg"><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>${problem}</div>`);
    return;
  }
  if (domain && domain.status !== 'available' && !table) {
    // 库读不到时目录里没有它的表清单：这不是「没有这张表」，是「现在看不了」
    setHtml(slot, html`<div class="page stack-lg">
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}"><a href="#/data">${t('data.title')}</a>
        <span aria-hidden="true">›</span><span>${domainTitle(domain)}</span></nav>
      <h1 class="page-title" tabindex="-1" data-role="view-title">${route.table}</h1>
      ${notice('danger', 'error', t('data.unavailable.title'), html`<p>${unavailableReason(domain)}</p><p>${t('data.unavailable.body')}</p>`)}
      <div class="row"><button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
        <a class="btn btn-sm btn-ghost" href="#/data">${icon('back', 'icon-sm')}${t('data.backToCatalog')}</a></div>
    </div>`);
    return;
  }
  if (!domain || !table) {
    setHtml(slot, html`<div class="page stack-lg">
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}"><a href="#/data">${t('data.title')}</a></nav>
      <h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.notFound.title')}</h1>
      ${notice('info', 'info', t('data.notFound.title'), html`<p>${t('data.notFound.body')}</p>`)}
      <p><a class="btn" href="#/data">${icon('back', 'icon-sm')}${t('data.backToCatalog')}</a></p>
    </div>`);
    return;
  }

  const available = table.status === 'available';
  const unavailableDomain = domain.status !== 'available';
  const columns = table.columns || [];
  const tab = view.tab;
  let body;
  if (tab === 'fields') {
    body = html`<div data-role="fields">${fieldsTab(table)}</div>`;
  } else if (!available || unavailableDomain) {
    body = html`<p class="muted">${t('data.noPreview')}</p>`;
  } else if (!table.previewable) {
    body = notice('info', 'info', t('data.previewUnsupported.title'), html`<p>${t(domain.preview_supported
      ? 'data.previewUnsupported.names' : 'data.previewUnsupported.body')}</p>`);
  } else {
    body = html`<div class="tb">
        <form class="tb-search" data-form="search" role="search">
          <label class="sr-only" for="tableSearch">${t('data.search')}</label>
          ${icon('search', 'icon-sm')}
          <input class="input" id="tableSearch" type="search" maxlength="100" value="${view.search}"
            placeholder="${t('data.searchPlaceholder')}" autocomplete="off">
        </form>
        <div class="tb-tools" data-role="tools">${tools()}</div>
      </div>
      <div data-role="panel">${panelBody(table)}</div>
      <div data-role="chips">${chips()}</div>
      <div data-role="insights">${insights(table)}</div>
      <div data-role="results">${results(table)}</div>`;
  }

  setHtml(slot, html`<div class="page page-wide stack">
    <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}">
      <a href="#/data">${t('data.title')}</a><span aria-hidden="true">›</span>
      <a href="${dataHref(domain.database)}">${domainTitle(domain)}</a><span aria-hidden="true">›</span><span>${labelOf(table)}</span>
    </nav>
    <div class="page-head">
      <div class="dx-title">${dbTile(domain.database, 'is-lg')}
        <div><h1 class="page-title" tabindex="-1" data-role="view-title">${labelOf(table)}</h1>
          <p class="tech">${domain.database}.${table.name} · ${t('data.readOnly')} · <span data-role="headmeta">${headMeta(table)}</span></p></div>
      </div>
      <div class="row">
        <button type="button" class="btn btn-sm" data-action="ask-about">${icon('ask', 'icon-sm')}${t('data.askAbout')}</button>
      </div>
    </div>
    <p class="page-sub">${tableSummary(table)}</p>
    ${relatedChips(table)}
    ${!available ? notice('warn', 'warn', t('data.missing.title'), html`<p>${t('data.missing.body')}</p>`) : ''}
    ${unavailableDomain ? notice('danger', 'error', t('data.unavailable.title'), html`<p>${t('data.unavailable.body')}</p>`) : ''}
    <div class="tabs" role="tablist" aria-label="${labelOf(table)}">
      <button type="button" class="tab" role="tab" id="tab-records" aria-controls="tabpanel" aria-selected="${String(tab === 'records')}"
        tabindex="${tab === 'records' ? '0' : '-1'}" data-action="tab" data-tab="records">${t('data.tab.records')}</button>
      <button type="button" class="tab" role="tab" id="tab-fields" aria-controls="tabpanel" aria-selected="${String(tab === 'fields')}"
        tabindex="${tab === 'fields' ? '0' : '-1'}" data-action="tab" data-tab="fields">${t('data.tab.fields', { n: columns.length })}</button>
    </div>
    <div id="tabpanel" role="tabpanel" aria-labelledby="tab-${tab}" class="stack">${body}</div>
    <div data-role="drawer">${drawer(table)}</div>
  </div>`);
  fitInsights();
}

/* ─── 工具栏、筛选面板、字段开关 ───────────────────────────────── */

function tools() {
  // 数的是看得见的条件：时间轴点出来的一对日期条件算一个
  const count = chipItems().length;
  const concealed = concealedColumns().length;
  const total = ((currentTable() || {}).columns || [])
    .filter((column) => column.status === 'available' && column.previewable !== false).length;
  return html`<button type="button" class="btn btn-sm" data-action="panel" data-name="filter" aria-expanded="${String(panel === 'filter')}">
      ${icon('filter', 'icon-sm')}${t('filter.title')}${count ? html`<span class="badge">${count}</span>` : ''}</button>
    <span class="tb-space"></span>
    ${profileSupport() === 'on' ? html`<button type="button" class="btn btn-sm btn-ghost" data-action="toggle-insights"
      aria-pressed="${String(insightsOn())}">${icon('chart', 'icon-sm')}${t('data.insights')}</button>` : ''}
    <button type="button" class="btn btn-sm btn-ghost" data-action="panel" data-name="columns" aria-expanded="${String(panel === 'columns')}">
      ${icon('columns', 'icon-sm')}${t('data.columns')}${concealed ? html`<span class="badge badge-soft">${total - concealed}/${total}</span>` : ''}</button>
    <button type="button" class="btn btn-sm btn-ghost" data-action="copy-page">${icon('copy', 'icon-sm')}${t('data.copyPage')}</button>`;
}

function filterForm(table) {
  const columns = (table.columns || [])
    .filter((column) => column.status === 'available' && column.previewable !== false);
  const chosen = columns.find((column) => column.name === draft.column) || columns[0];
  const allowed = serverOperators();
  const operators = operatorsFor(chosen).filter((op) => !allowed || allowed.includes(op));
  const op = operators.includes(draft.op) ? draft.op : operators[0];
  const full = view.filters.length >= maxFilters();
  // 这个字段有哪些取值是知道的（整张表的取值清单）：做成输入提示，少打字也少打错
  const profile = baseData();
  const facet = profile && chosen && profile.facets.find((item) => item.column === chosen.name);
  const known = facet ? facet.values.filter((item) => !item.truncated && item.count > 0) : [];

  return html`<form class="filters panel" data-form="filter">
    <div class="field"><label for="filterColumn">${t('filter.field')}</label>
      <select class="select" id="filterColumn" data-input="filter-column">${columns.map((column) => html`<option
        value="${column.name}" ${chosen && column.name === chosen.name ? 'selected' : ''}>${labelOf(column)}</option>`)}</select></div>
    <div class="field"><label for="filterOp">${t('filter.condition')}</label>
      <select class="select" id="filterOp" data-input="filter-op">${operators.map((item) => html`<option
        value="${item}" ${item === op ? 'selected' : ''}>${t('filter.op.' + item)}</option>`)}</select></div>
    <div class="field field-wide" ${VALUELESS.includes(op) ? 'hidden' : ''}><label for="filterValue">${t('filter.value')}</label>
      <input class="input" id="filterValue" maxlength="200" autocomplete="off"
        ${chosen && chosen.kind === 'date' ? html`type="date"` : ''}
        ${chosen && chosen.kind === 'number' ? html`inputmode="decimal"` : ''}
        ${known.length ? html`list="filterValues"` : ''}
        placeholder="${chosen && chosen.value_range && !known.length ? chosen.value_range : ''}">
      ${known.length ? html`<datalist id="filterValues">${known.map((item) => html`<option value="${item.value}"></option>`)}</datalist>` : ''}</div>
    <button type="submit" class="btn" ${full ? 'disabled' : ''}>${icon('plus', 'icon-sm')}${t('filter.add')}</button>
    ${full ? html`<p class="muted panel-note">${t('filter.tooMany', { n: maxFilters() })}</p>` : ''}
  </form>`;
}

/* 表格里现在没画出来的字段：手动收起的，加上（没选「全部显示」时）整张表都没内容的。 */
function concealedColumns() {
  const names = new Set(hidden);
  if (!showEmpty) emptyColumns().forEach((name) => names.add(name));
  return [...names].filter((name) => columnEntry(name));
}

function columnsForm(table) {
  const blank = new Set(emptyColumns());
  const columns = (table.columns || []).filter((column) => column.status === 'available' && column.previewable !== false);
  return html`<div class="panel">
    <fieldset class="column-picks"><legend class="sr-only">${t('data.columns')}</legend>
      ${columns.map((column) => {
        const off = hidden.has(column.name) || (!showEmpty && blank.has(column.name));
        return html`<label class="check"><input type="checkbox" data-input="column-toggle" data-name="${column.name}" ${off ? '' : 'checked'}>
          <span>${labelOf(column)}</span>${blank.has(column.name) ? html`<span class="muted">${t('data.columnEmpty')}</span>` : ''}</label>`;
      })}
    </fieldset>
    <div class="row">
      ${concealedColumns().length ? html`<button type="button" class="link-btn" data-action="show-all-columns">${t('data.showAllColumns')}</button>` : ''}
      <span class="muted">${t('data.columnsNote')}</span>
    </div>
  </div>`;
}

const panelBody = (table) => (panel === 'filter' ? filterForm(table) : panel === 'columns' ? columnsForm(table) : '');

function chips() {
  const items = chipItems();
  if (!items.length && !view.search) return '';
  return html`<div class="filter-chips" aria-label="${t('filter.active')}">
    ${view.search ? html`<span class="filter-chip">${t('filter.searchChip', { term: view.search })}
      <button type="button" data-action="clear-search" title="${t('filter.remove')}"
        aria-label="${t('filter.removeNamed', { name: t('filter.searchChip', { term: view.search }) })}">${icon('close', 'icon-sm')}</button></span>` : ''}
    ${items.map((item) => html`<span class="filter-chip">${item.label}
      <button type="button" data-action="remove-filter" ${item.column !== undefined
        ? html`data-column="${item.column}"` : html`data-index="${item.index}"`} title="${t('filter.remove')}"
        aria-label="${t('filter.removeNamed', { name: item.label })}">${icon('close', 'icon-sm')}</button></span>`)}
    <button type="button" class="link-btn" data-action="clear-filters">${t('filter.clear')}</button>
  </div>`;
}

/* ─── 概览 ─────────────────────────────────────────────────────── */

function summaryCard(table, profile) {
  const base = baseData();
  const total = profile.total_rows;
  const whole = base && typeof base.total_rows === 'number' ? base.total_rows : null;
  const stats = new Map(profile.columns.map((column) => [column.name, column]));
  // 合计只给「语义层标成指标、而且写了单位」的数字：单位都没有的数加在一起，没人知道加出来的是什么
  const measures = (table.columns || [])
    .filter((column) => column.unit && typeof (stats.get(column.name) || {}).sum === 'number')
    .slice(0, 2);
  return html`<section class="insight insight-summary">
    <h3 class="insight-title">${t(profile.filtered ? 'viz.matching' : 'viz.records')}</h3>
    <p class="hero" title="${typeof total === 'number' ? formatCount(total) : ''}">${typeof total === 'number' ? formatCompact(total) : '—'}</p>
    ${profile.filtered && whole !== null ? html`<p class="muted insight-sub">${t('viz.ofTotal', {
      total: formatCount(whole), share: formatShare(total || 0, whole) || '0%',
    })}</p>` : ''}
    ${measures.length ? html`<dl class="measures">${measures.map((column) => {
      const item = stats.get(column.name);
      const average = item.filled ? item.sum / item.filled : null;
      return html`<div><dt>${labelOf(column)} <span class="th-unit">(${column.unit})</span></dt>
        <dd title="${formatNumber(item.sum, { unit: column.unit })}"><span class="muted">${t('viz.sum')}</span> ${formatCompact(item.sum, { unit: column.unit })}
          ${average !== null ? html`<span class="muted measure-avg">${t('viz.average')}</span> ${formatCompact(average, { unit: column.unit })}` : ''}</dd></div>`;
    })}</dl>` : ''}
  </section>`;
}

function facetCard(facet, rows) {
  const entry = columnEntry(facet.column);
  const slots = slotsFor(facet.column);
  const active = view.filters.find((filter) => filter.column === facet.column && filter.op === 'eq');
  const title = entry ? labelOf(entry) : facet.column;
  const listed = facet.values.reduce((sum, item) => sum + item.count, 0);
  const colored = slots.size > 0 && facet.values.every((item) => slots.has(String(item.value)));
  const shown = colored ? facet.values : facet.values.slice(0, TOP_VALUES);
  const items = shown.map((item) => ({
    value: item.value,
    text: String(item.value),
    count: item.count,
    slot: colored ? slots.get(String(item.value)) : 0,
    active: Boolean(active && sameValue(active.value, item.value)),
    truncated: Boolean(item.truncated),
    muted: Boolean(active && !sameValue(active.value, item.value)),
  }));
  // 占比按「列出来的这些取值」算；没列全的（只列了最多的几个）按这批记录的总数算
  const whole = facet.complete ? listed : Math.max(rows || 0, listed);
  const more = facet.distinct !== null && facet.distinct > shown.length;
  return html`<section class="insight">
    <h3 class="insight-title">${title}${facet.own_filters_excluded
      ? html`<span class="insight-flag" title="${t('viz.ownFilterHint')}">${t('viz.ownFilter')}</span>` : ''}</h3>
    ${colored ? stackedBar(items, title) : ''}
    ${valueList({ column: facet.column, items, total: whole, action: 'facet', bars: !colored })}
    ${more ? html`<p class="muted insight-note">${t('viz.topValues', { shown: shown.length, total: formatCount(facet.distinct) })}</p>` : ''}
  </section>`;
}

function timelineCard(line) {
  const entry = columnEntry(line.column);
  const title = entry ? labelOf(entry) : line.column;
  const { grain, bars } = rebucket(line.buckets);
  const peak = bars.reduce((best, bar) => (bar.count > best.count ? bar : best), bars[0]);
  return html`<section class="insight">
    <h3 class="insight-title">${title}<span class="insight-flag">${t('viz.grain.' + grain)}</span></h3>
    ${columnChart({ column: line.column, bars, label: t('viz.timelineLabel', { column: title }), action: 'period', selected: dateRange(line.column) })}
    <p class="muted insight-note">${t('viz.peak', { label: peak.label, n: formatCount(peak.count) })}${line.unparsed
      ? ' · ' + t('viz.unparsed', { n: formatCount(line.unparsed) }) : ''}${line.truncated ? ' · ' + t('viz.truncated') : ''}</p>
  </section>`;
}

/* 一个字段的分布值不值得占一张卡片。取值没取全、而且最多的那个也只出现一两次的
   （地址、编号、备注），列出来只是一串「各 1 条」。正筛着的字段一定留着——得让人能取消。 */
function worthShowing(facet, rows) {
  if (view.filters.some((filter) => filter.column === facet.column)) return true;
  if (!facet.values.some((item) => item.count > 0)) return false;
  if (facet.complete) return true;
  return facet.values[0].count >= Math.max(2, (rows || 0) * 0.005);
}

function insightCards(table, profile) {
  const rows = profile.total_rows || 0;
  const facets = profile.facets.filter((facet) => worthShowing(facet, rows));
  const timelines = profile.timelines.filter((line) => line.buckets.length);
  // 声明过取值的（状态、类型）先出，其次时间轴，再是别的分布
  const declared = facets.filter((facet) => facet.ordered_by === 'declared');
  const others = facets.filter((facet) => facet.ordered_by !== 'declared');
  return [
    summaryCard(table, profile),
    ...declared.map((facet) => facetCard(facet, rows)),
    ...timelines.map(timelineCard),
    ...others.map((facet) => facetCard(facet, rows)),
  ];
}

function insights(table) {
  const support = profileSupport();
  if (support === 'unsupported') {
    return html`<p class="muted insight-hint">${icon('info', 'icon-sm')} ${t('viz.unsupported')}</p>`;
  }
  if (support !== 'on' || !insightsOn()) return '';

  const entry = viewEntry();
  if (!entry) return '';
  if (entry.status === 'error') {
    // 这组条件下的统计没算出来：不拿上一组条件的数字顶在这里
    const error = entry.error || {};
    const key = has('data.error.' + error.code) ? 'data.error.' + error.code
      : error.status === 0 ? 'error.network' : 'viz.failed';
    return html`<div class="insight-hint"><span class="muted">${icon('info', 'icon-sm')} ${t('viz.failedTitle')} ${t(key)}</span>
      <button type="button" class="link-btn" data-action="retry-profile">${t('action.tryAgain')}</button></div>`;
  }
  const profile = entry.status === 'ready' ? entry.data : lastProfile;
  if (!profile) {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('viz.loading')}</div>`;
  }
  if (entry.status === 'ready') lastProfile = entry.data;

  const cards = insightCards(table, profile);
  const skipped = (profile.skipped || []).length;
  return html`<section class="insights" aria-label="${t('data.insights')}" aria-busy="${String(entry.status === 'loading')}">
    <div class="insight-grid${expanded ? '' : ' is-collapsed'}">${cards}</div>
    <div class="insight-foot">
      <span class="muted">${t(profile.filtered ? 'viz.scopeFiltered' : 'viz.scopeAll')}${skipped
        ? ' · ' + tn('viz.skipped', skipped) : ''}</span>
      ${cards.length > 1 ? html`<button type="button" class="link-btn" data-action="toggle-expand" aria-expanded="${String(expanded)}">${t(expanded
        ? 'viz.showLess' : 'viz.showAll', { n: cards.length })}</button>` : ''}
    </div>
    ${state.learning ? html`<section class="learn"><div class="learn-head"><span class="row">${icon('learn')}${t('learn.profile.title')}</span>
        <span class="chip chip-info chip-wrap">POST /catalog/${route.database}/${route.table}/profile</span></div>
      <div class="learn-body stack">
        <p class="muted">${t('learn.profile.sql', { n: profile.sql.length, dialect: profile.dialect })}</p>
        <pre class="code">${profile.sql.join('\n\n')}</pre>
        <p class="learn-note">${t('learn.profile.note')}</p>
        <p class="learn-src">src/ai_sql_agent/catalog/profile.py · catalog/service.py · api/catalog.py · auth/enforcement.py</p>
      </div></section>` : ''}
  </section>`;
}

/* ─── 记录 ─────────────────────────────────────────────────────── */

function gridColumns(table, data) {
  const entries = new Map((table.columns || []).map((column) => [column.name, column]));
  const concealed = new Set(concealedColumns());
  return data.columns
    .map((column, index) => ({ column, index }))
    .filter(({ column }) => !concealed.has(column.name))
    .map(({ column, index }) => {
      const entry = entries.get(column.name);
      const slots = slotsFor(column.name);
      return {
        key: column.name,
        index,
        label: entry ? labelOf(entry) : column.name,
        unit: entry ? entry.unit : '',
        numeric: column.kind === 'number' && !slots.size,
        sortable: column.sortable !== false,
        plain: Boolean(entry && (entry.role === 'join_key' || entry.primary_key)),
        hint: entry ? descriptionOf(entry).text : '',
        slots: slots.size ? slots : null,
        clip: column.kind !== 'number' && column.kind !== 'date',
      };
    });
}

function previewError(error) {
  const code = error.code || '';
  const key = has('data.error.' + code) ? 'data.error.' + code
    : error.status === 0 ? 'error.network'
      : error.status === 401 ? 'error.401'
        : error.status === 403 ? 'error.403'
          : 'data.error.failed';
  return html`${notice('danger', 'error', t('data.error.previewTitle'), html`<p>${t(key)}</p>
      <p class="original">${error.status ? `HTTP ${error.status}` : t('error.noResponse')}${code ? ` · ${code}` : ''}${error.serverMessage
        ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
    <div class="row"><button type="button" class="btn btn-sm" data-action="retry-preview">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button></div>`;
}

function results(table) {
  if (preview.status === 'error') return previewError(preview.error);
  if (!preview.data) {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('data.loadingRows')}</div>`;
  }

  const data = preview.data;
  const filtered = isFiltered();
  const columns = gridColumns(table, data);
  const from = data.offset + 1;
  const to = data.offset + data.rows.length;
  const entries = new Map((table.columns || []).map((column) => [column.name, column]));
  const dates = (data.date_ranges || []).map((range) =>
    `${entries.has(range.column) ? labelOf(entries.get(range.column)) : range.column} ${formatRange(range)}`);
  const pages = data.total_rows != null ? Math.max(1, Math.ceil(data.total_rows / data.page_size)) : null;
  // 整张表都没内容的字段默认不占列宽；在这里说一声，想看随时能打开
  const blank = showEmpty ? [] : emptyColumns().filter((name) => !hidden.has(name) && columnEntry(name));

  const count = data.rows.length === 0
    ? ''
    : data.total_rows != null
      ? t('data.range', { from: formatCount(from), to: formatCount(to), total: formatCount(data.total_rows) })
      : t('data.rangeUnknown', { from: formatCount(from), to: formatCount(to) });

  let body;
  if (!data.rows.length && filtered) {
    body = html`<div class="empty-state"><strong>${t('data.noMatch.title')}</strong>${t('data.noMatch.body')}
      <div><button type="button" class="btn btn-sm" data-action="clear-filters">${t('filter.clear')}</button></div></div>`;
  } else if (!data.rows.length && data.offset > 0) {
    body = html`<div class="empty-state"><strong>${t('data.pastEnd.title')}</strong>
      <div><button type="button" class="btn btn-sm" data-action="page" data-page="1">${t('data.firstPage')}</button></div></div>`;
  } else if (!data.rows.length) {
    body = html`<div class="empty-state"><strong>${t('data.emptyTable.title')}</strong>${t('data.emptyTable.body')}</div>`;
  } else {
    body = renderTable({
      columns,
      rows: data.rows,
      sort: view.sort,
      sortAction: 'sort',
      label: t('data.tableLabel', { table: labelOf(table) }),
      rowAction: 'open-row',
      selected: detail ? detail.index : -1,
    });
  }

  return html`<div class="stack-sm${preview.status === 'loading' ? ' is-stale' : ''}" aria-busy="${preview.status === 'loading' ? 'true' : 'false'}">
    <div class="result-meta" role="status">
      ${count ? html`<span><strong>${count}</strong></span>` : ''}
      ${data.rows.length || dates.length ? html`<span><strong>${t('meta.recordDates')}:</strong> ${dates.length
        ? dates.join('; ') : t('data.noDateFields')}</span>` : ''}
      <span title="${t('meta.readHint')}">${data.as_of_source === 'snapshot'
        ? t('meta.readSnapshot', { time: formatTime(data.as_of) })
        : data.as_of ? t('meta.readLive', { time: formatTime(data.as_of) }) : t('meta.readUnknown')}</span>
      ${blank.length ? html`<span>${tn('data.emptyHidden', blank.length)}
        <button type="button" class="link-btn" data-action="show-empty">${t('data.showThem')}</button></span>` : ''}
      ${preview.status === 'loading' ? html`<span class="row"><span class="spinner"></span>${t('data.loadingRows')}</span>` : ''}
    </div>
    ${data.columns_hidden ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.columnsHidden')}</p>` : ''}
    ${(data.columns_unsupported || []).length ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.columnsUnsupported', {
      names: data.columns_unsupported.join(', '),
    })}</p>` : ''}
    ${data.search_columns_limited ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.searchLimited', {
      n: (data.search_columns || []).length,
    })}</p>` : ''}
    ${data.cells_truncated ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.cellsTruncated', { n: data.cell_limit })}</p>` : ''}
    ${data.stable_order === false && (data.has_more || data.page > 1)
      ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.unstableOrder')}</p>` : ''}
    ${body}
    <div class="table-foot">
      <span class="row">
        <label class="row">${t('data.pageSize')}
          <select class="select select-auto" data-input="page-size">${PAGE_SIZES.map((size) => html`<option
            value="${size}" ${size === view.pageSize ? 'selected' : ''}>${size}</option>`)}</select></label>
        ${data.rows.length ? html`<span class="muted">${t('data.rowHint')}</span>` : ''}
      </span>
      <span class="pager">
        <button type="button" class="btn btn-sm" data-action="page" data-which="prev" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>${icon('chevronLeft', 'icon-sm')}${t('table.prev')}</button>
        <span>${pages ? t('table.pageOf', { page: formatCount(data.page), pages: formatCount(pages) }) : t('data.pageNumber', { page: data.page })}</span>
        <button type="button" class="btn btn-sm" data-action="page" data-which="next" data-page="${data.page + 1}" ${data.has_more ? '' : 'disabled'}>${t('table.next')}${icon('chevronRight', 'icon-sm')}</button>
      </span>
    </div>
    ${state.learning ? html`<section class="learn"><div class="learn-head"><span class="row">${icon('learn')}${t('learn.preview.title')}</span>
        <span class="chip chip-info chip-wrap">POST /catalog/${route.database}/${route.table}/rows</span></div>
      <div class="learn-body stack">
        <div class="row-between"><span class="muted">${t('learn.preview.sql', { dialect: data.dialect })}</span>
          <button type="button" class="btn btn-sm btn-ghost" data-action="copy-sql">${icon('copy', 'icon-sm')}${t('action.copySql')}</button></div>
        <pre class="code">${data.sql}</pre>
        <dl class="kv"><dt>${t('process.traceId')}</dt><dd><span class="mono">${data.trace_id}</span></dd></dl>
        <p class="learn-note">${t('learn.preview.note')}</p>
        <p class="learn-src">src/ai_sql_agent/catalog/preview.py · catalog/service.py · execution/sqlite_executor.py · auth/enforcement.py</p>
      </div></section>` : ''}
  </div>`;
}

/* ─── 字段说明 ─────────────────────────────────────────────────── */

function fieldItem(column, stats, total) {
  const role = column.role && has('role.' + column.role) ? t('role.' + column.role) : '';
  const note = descriptionOf(column);
  const usage = localized(column, 'usage');
  // 数据字典里当前语言能读的说明排最前；只有原文的那几段，先给界面语言能读的
  // 结构化信息（角色、取值、关联），原文退到后面。说明和用途各自判断，互不连带。
  // 有的数据字典把用途原样接在说明后面：说明里已经有的那句用途不再重复一遍
  const dictionary = [note, usage.text && !note.text.includes(usage.text) ? usage : null]
    .filter((item) => item && item.text);
  const paragraph = (item) => html`<p ${item.original
    ? html`class="original-note" lang="zh" title="${t('data.originalNote')}"` : ''}>${item.text}</p>`;
  const readable = dictionary.filter((item) => !item.original).map(paragraph);
  const originals = dictionary.filter((item) => item.original).map(paragraph);
  const filled = stats && typeof total === 'number' && total > 0;
  const range = stats && stats.min !== undefined
    ? (typeof stats.min === 'number'
      ? `${formatNumber(stats.min, { unit: column.unit })} – ${formatNumber(stats.max, { unit: column.unit })}`
      : formatRange({ min: String(stats.min).slice(0, 10), max: String(stats.max).slice(0, 10) }))
    : '';
  return html`<li>
    <div>
      <div class="field-name">${labelOf(column)}</div>
      <div class="tech">${column.name}</div>
      <div class="field-facts">
        ${column.status === 'missing' ? html`<span class="chip chip-warn">${t('data.missing')}</span>` : ''}
        ${column.primary_key ? html`<span class="chip">${icon('key', 'icon-sm')}${t('data.key')}</span>` : ''}
        ${column.status !== 'missing' && column.previewable === false ? html`<span class="chip chip-warn">${t('data.notInPreview')}</span>` : ''}
        ${has('kind.' + column.kind) && column.status !== 'missing' ? html`<span class="chip">${t('kind.' + column.kind)}</span>` : ''}
        ${column.unit ? html`<span class="chip">${t('data.unit', { unit: column.unit })}</span>` : ''}
        ${column.data_type ? html`<span class="tech">${column.data_type}</span>` : ''}
      </div>
    </div>
    <div class="field-desc">
      ${readable}
      ${role ? html`<p>${role}</p>` : ''}
      ${column.value_range ? html`<p><span class="muted">${t('data.values')}</span> <span class="mono">${column.value_range}</span></p>` : ''}
      ${(column.references || []).map((reference) => {
        const target = findTable(reference.database, reference.table);
        const domain = findDomain(reference.database);
        const name = `${domain ? domainTitle(domain) : reference.database} › ${target ? labelOf(target) : reference.table} › ${reference.column}`;
        const remark = localized(reference, 'note');
        return html`<p>${icon('link', 'icon-sm')} <span class="muted">${t(reference.relation === 'cross_database' ? 'data.linkedAcross' : 'data.linked')}</span>
          <a href="${dataHref(reference.database, reference.table)}">${name}</a>
          ${remark.text ? html`<span ${remark.original ? html`class="original-note" lang="zh"` : html`class="muted"`}>${remark.text}</span>` : ''}</p>`;
      })}
      ${originals}
      ${!dictionary.length ? html`<p class="muted">${t(column.documented ? 'data.noDescription' : 'data.undocumented')}</p>` : ''}
    </div>
    <div class="field-fill">${filled ? html`
      <div class="fill-line">${meter(stats.filled, total, t('viz.fillLabel', { share: formatShare(stats.filled, total) || '0%' }))}
        <span class="fill-share">${formatShare(stats.filled, total) || '0%'}</span></div>
      <p class="muted">${t('viz.filled', { filled: formatCount(stats.filled), total: formatCount(total) })}</p>
      ${range ? html`<p class="muted">${t('viz.range')} ${range}</p>` : ''}` : ''}
    </div>
  </li>`;
}

function fieldsTab(table) {
  const profile = baseData();
  const stats = new Map((profile ? profile.columns : []).map((column) => [column.name, column]));
  const total = profile ? profile.total_rows : null;
  return html`<ul class="field-list has-fill">${(table.columns || []).map((column) => fieldItem(column, stats.get(column.name), total))}</ul>`;
}

/* ─── 一条记录 ─────────────────────────────────────────────────── */

const relatedKey = (member, column) => [column, member.database, member.table, member.column].join('\u0000');

/* 这条记录能去哪些表里找对得上的记录：[{ column, value, other }]。 */
function relatedTargets(table, row, names) {
  const links = buildLinks(catalog.data.domains);
  const targets = [];
  relatedTables(links, route.database, table.name).forEach((item) => {
    const position = names.indexOf(item.column);
    const value = position === -1 ? null : row[position];
    if (value === null || value === undefined || value === '') return;
    item.others.forEach((other) => targets.push({ column: item.column, value, other }));
  });
  return targets;
}

/* 相关表里挑几个字段出来看（抽屉里放不下整张表）。用来对上的那个字段不用再列——每一行都是同一个值。 */
function briefColumns(tableEntry, data, skip) {
  const entries = new Map((tableEntry.columns || []).map((column) => [column.name, column]));
  const rank = (entry) => (entry && entry.role in BRIEF_RANK ? BRIEF_RANK[entry.role] : 4);
  const picked = data.columns
    .map((column, index) => ({ column, index, entry: entries.get(column.name) }))
    .filter(({ column }) => column.name !== skip && column.kind !== 'binary')
    .sort((a, b) => rank(a.entry) - rank(b.entry) || a.index - b.index)
    .slice(0, RELATED_COLUMNS)
    .sort((a, b) => a.index - b.index);
  return picked.map(({ column, index, entry }) => ({
    key: column.name,
    index,
    label: entry ? labelOf(entry) : column.name,
    unit: entry ? entry.unit : '',
    numeric: column.kind === 'number',
    plain: Boolean(entry && (entry.role === 'join_key' || entry.primary_key)),
    clip: column.kind !== 'number' && column.kind !== 'date',
  }));
}

function relatedSection(target, table) {
  const { other, column, value } = target;
  const state_ = detail.related.get(relatedKey(other, column)) || { status: 'idle' };
  const href = dataHref(other.database, other.table, { filters: [{ column: other.column, op: 'eq', value: String(value) }] });
  // 两边的字段叫法一样就只写一次（订单号）；不一样才写成「物料名称 = 补件名称」
  const mine = labelOf(columnEntry(column) || { name: column });
  const theirs = labelOf(other.columnEntry);
  const via = mine === theirs ? mine : `${mine} = ${theirs}`;
  let body;
  let count = '';
  if (state_.status === 'error') {
    const code = (state_.error && state_.error.code) || '';
    body = html`<p class="muted">${t(has('data.error.' + code) ? 'data.error.' + code : 'data.error.failed')}
      <button type="button" class="link-btn" data-action="retry-related">${t('action.tryAgain')}</button></p>`;
  } else if (state_.status !== 'ready') {
    body = html`<div class="loading" role="status"><span class="spinner"></span>${t('data.loadingRows')}</div>`;
  } else if (!state_.data.rows.length) {
    body = html`<p class="muted">${t('data.related.none')}</p>`;
    count = formatCount(0);
  } else {
    const data = state_.data;
    // 那张表的配色到了就用上：同一个状态在它自己的页面和这里是同一个颜色
    const profile = peekProfile(other.database, other.table);
    const columns = briefColumns(other.tableEntry, data, other.column).map((item) => {
      const slots = profile ? slotsFor(item.key, profile) : null;
      return slots && slots.size ? { ...item, slots, numeric: false } : item;
    });
    const total = data.total_rows != null ? data.total_rows : data.rows.length;
    count = `${formatCount(total)}${data.total_rows == null && data.has_more ? '+' : ''}`;
    body = html`${renderTable({ columns, rows: data.rows, label: t('data.tableLabel', { table: labelOf(other.tableEntry) }) })}
      ${total > data.rows.length || data.has_more ? html`<p class="rel-more"><a href="${href}">${t('data.related.all', { n: count })} ${icon('chevronRight', 'icon-sm')}</a></p>` : ''}`;
  }
  return html`<section class="rel">
    <header class="rel-head">
      <a class="rel-name" href="${href}">${dbTile(other.database, 'is-xs')}${labelOf(other.tableEntry)}</a>
      <span class="muted rel-via">${t('data.related.via', { key: via })}</span>
      ${count ? html`<span class="badge">${count}</span>` : ''}
    </header>
    ${body}
  </section>`;
}

function drawer(table) {
  if (!detail || !preview.data || !preview.data.rows[detail.index]) return '';
  const data = preview.data;
  const row = data.rows[detail.index];
  const names = data.columns.map((column) => column.name);
  const entries = new Map((table.columns || []).map((column) => [column.name, column]));
  const keyIndex = names.findIndex((name) => (entries.get(name) || {}).primary_key);
  const lead = row[keyIndex === -1 ? 0 : keyIndex];
  const filled = [];
  const blank = [];
  names.forEach((name, index) => {
    const entry = entries.get(name);
    const label = entry ? labelOf(entry) : name;
    const cell = formatCell(row[index], {
      unit: entry ? entry.unit : '',
      plain: Boolean(entry && (entry.role === 'join_key' || entry.primary_key)),
      kind: data.columns[index].kind === 'number' ? 'number' : '',
    });
    if (cell.empty) { blank.push(label); return; }
    const slot_ = slotsFor(name).get(String(row[index]));
    filled.push(html`<div><dt>${label}${entry && entry.unit ? html` <span class="th-unit">(${entry.unit})</span>` : ''}
        <span class="tech">${name}</span></dt>
      <dd class="${cell.numeric ? 'num-value' : ''}">${slot_ ? valuePill(cell.text, slot_) : cell.text}</dd></div>`);
  });
  const targets = relatedTargets(table, row, names);
  const position = data.offset + detail.index + 1;
  return html`<aside class="drawer" aria-label="${t('data.record.title')}">
    <header class="drawer-head">
      <div class="grow">
        <p class="drawer-kicker">${dbTile(route.database, 'is-xs')}${labelOf(table)} · ${t('data.record.position', { n: formatCount(position) })}</p>
        <h2 class="drawer-title" tabindex="-1" data-role="detail-title">${formatCell(lead).text}</h2>
      </div>
      <div class="drawer-tools">
        <button type="button" class="icon-btn" data-action="row-step" data-which="prev" title="${t('data.record.prev')}"
          aria-label="${t('data.record.prev')}" ${detail.index <= 0 ? 'disabled' : ''}>${icon('chevronLeft')}</button>
        <button type="button" class="icon-btn" data-action="row-step" data-which="next" title="${t('data.record.next')}"
          aria-label="${t('data.record.next')}" ${detail.index >= data.rows.length - 1 ? 'disabled' : ''}>${icon('chevronRight')}</button>
        <button type="button" class="icon-btn" data-action="close-row" title="${t('action.close')}" aria-label="${t('action.close')}">${icon('close')}</button>
      </div>
    </header>
    <div class="drawer-body">
      <dl class="rec">${filled}</dl>
      ${blank.length ? html`<p class="muted rec-blank">${t('data.record.blank', { names: blank.join(t('common.listSep')) })}</p>` : ''}
      ${targets.length ? html`<h3 class="drawer-section">${icon('link', 'icon-sm')}${t('data.related.title')}</h3>
        <p class="muted drawer-lead">${t('data.related.lead')}</p>
        ${targets.slice(0, MAX_RELATED).map((target) => relatedSection(target, table))}
        ${targets.length > MAX_RELATED ? html`<p class="muted">${t('data.related.more', { n: targets.length - MAX_RELATED })}</p>` : ''}` : ''}
    </div>
  </aside>`;
}

/* ─── 局部重画 ─────────────────────────────────────────────────── */

function redraw(role, content) {
  const node = slot && slot.querySelector(`[data-role="${role}"]`);
  const table = currentTable();
  if (node && table) setHtml(node, content(table));
}

const drawTools = () => redraw('tools', tools);
const drawPanel = () => redraw('panel', panelBody);
const drawChips = () => redraw('chips', chips);
const drawDrawer = () => redraw('drawer', drawer);
const drawFields = () => redraw('fields', fieldsTab);

/* 概览收着的时候只露第一排。一排就放得下全部卡片时，「显示全部」这个按钮没有意义，收起来。 */
function fitInsights() {
  const grid = slot && slot.querySelector('.insight-grid');
  const toggle = slot && slot.querySelector('[data-action="toggle-expand"]');
  if (grid && toggle && !expanded) toggle.hidden = grid.scrollHeight <= grid.clientHeight + 4;
}

function drawInsights() {
  redraw('insights', insights);
  fitInsights();
}

/* 重画记录。表格自己的滚动位置要留住：横向的一直留（看的是右边那几列，翻一页不该弹回最左），
   纵向的只在「同一页重画」时留——换了页就该从第一行看起。 */
function drawResults({ samePage = true } = {}) {
  const before = slot && slot.querySelector('[data-role="results"] .table-wrap');
  const offset = before ? { left: before.scrollLeft, top: before.scrollTop } : null;
  redraw('results', results);
  const after = slot && slot.querySelector('[data-role="results"] .table-wrap');
  if (!offset || !after) return;
  after.scrollLeft = offset.left;
  if (samePage) after.scrollTop = offset.top;
}

/* 打开了哪一行，直接标在现有的行上，不为这个重画整张表。
   用「下一条」翻出了表格可见的范围时，表格自己跟着滚（只滚表格，不动整页）。 */
function markSelected() {
  if (!slot) return;
  slot.querySelectorAll('tr[data-action="open-row"]').forEach((row) => {
    if (!detail || Number(row.dataset.index) !== detail.index) { row.removeAttribute('aria-current'); return; }
    row.setAttribute('aria-current', 'true');
    const wrap = row.closest('.table-wrap');
    const head = wrap ? wrap.querySelector('thead') : null;
    if (!wrap) return;
    const top = row.offsetTop - (head ? head.offsetHeight : 0);
    if (top < wrap.scrollTop) wrap.scrollTop = top;
    else if (row.offsetTop + row.offsetHeight > wrap.scrollTop + wrap.clientHeight) {
      wrap.scrollTop = row.offsetTop + row.offsetHeight - wrap.clientHeight;
    }
  });
}

/* 地址跟着视图走（不产生新的历史记录）：刷新、复制链接都还是现在这个样子。 */
function syncAddress() {
  const href = dataHref(route.database, route.table, view);
  if (location.hash !== href) history.replaceState(history.state, '', href);
}

async function loadPreview() {
  const table = currentTable();
  if (!usable(table, currentDomain())) return;
  if (controller) controller.abort();
  controller = new AbortController();
  const ticket = ++sequence;
  preview = { ...preview, status: 'loading', error: null };
  closeRow({ focus: false });
  drawResults();

  try {
    const data = await api.rows(route.database, route.table, {
      page: view.page,
      page_size: view.pageSize,
      search: view.search,
      filters: view.filters,
      sort: view.sort ? { column: view.sort.key, direction: view.sort.direction } : null,
    }, controller.signal);
    if (ticket !== sequence) return;       // 更晚发出的请求已经接管了
    preview = { status: 'ready', data, error: null };
    announce(data.rows.length
      ? (data.total_rows != null
        ? t('data.range', { from: data.offset + 1, to: data.offset + data.rows.length, total: data.total_rows })
        : t('data.rangeUnknown', { from: data.offset + 1, to: data.offset + data.rows.length }))
      : t(isFiltered() ? 'data.noMatch.title' : 'data.emptyTable.title'));
  } catch (error) {
    if ((error && error.name === 'AbortError') || ticket !== sequence) return;
    preview = { status: 'error', data: null, error };
    announce(t('data.error.previewTitle'));
  }
  drawResults({ samePage: false });
}

/* 视图变了（搜索、筛选、排序、翻页）：地址、标签、概览、记录一起跟上。 */
function applyView(change) {
  const scoped = 'filters' in change || 'search' in change;
  view = { ...view, ...change };
  syncAddress();
  if (scoped) {
    refreshProfiles();
    drawTools();
    drawPanel();
    drawChips();
    drawInsights();
  }
  loadPreview();
}

/* ─── 记录详情：打开、关闭、取相关记录 ─────────────────────────── */

function loadRelated({ fresh = false } = {}) {
  const table = currentTable();
  if (!detail || !table || !preview.data) return;
  const row = preview.data.rows[detail.index];
  const names = preview.data.columns.map((column) => column.name);
  const opened = detail;
  if (relatedFor !== catalog.data) { relatedFor = catalog.data; relatedCache.clear(); }
  relatedTargets(table, row, names).slice(0, MAX_RELATED).forEach((target) => {
    const key = relatedKey(target.other, target.column);
    if (detail.related.has(key)) return;
    detail.tables.add(target.other.database + '\u0000' + target.other.table);
    requestProfile(target.other.database, target.other.table);

    const cacheKey = key + '\u0000' + String(target.value);
    const cached = relatedCache.get(cacheKey);
    if (!fresh && cached && Date.now() - cached.at < RELATED_TTL_MS) {
      detail.related.set(key, { status: 'ready', data: cached.data });
      return;
    }
    detail.related.set(key, { status: 'loading' });
    api.rows(target.other.database, target.other.table, {
      page: 1,
      page_size: RELATED_ROWS,
      filters: [{ column: target.other.column, op: 'eq', value: String(target.value) }],
    }, detailAbort.signal)
      .then((data) => {
        opened.related.set(key, { status: 'ready', data });
        relatedCache.set(cacheKey, { at: Date.now(), data });
        if (relatedCache.size > RELATED_KEEP) relatedCache.delete(relatedCache.keys().next().value);
      })
      .catch((error) => {
        if (error && error.name === 'AbortError') return;
        opened.related.set(key, { status: 'error', error });
      })
      .finally(() => { if (detail === opened) drawDrawer(); });
  });
}

function openRow(index, { focus = true, settle = 0 } = {}) {
  if (!preview.data || !preview.data.rows[index]) return;
  if (detailAbort) detailAbort.abort();
  clearTimeout(detailTimer);
  detailAbort = new AbortController();
  detail = { index, related: new Map(), tables: new Set() };
  const opened = detail;
  // settle：连着翻的时候等人停下来再查。字段内容是现成的，马上就画
  if (settle) detailTimer = setTimeout(() => { if (detail === opened) { loadRelated(); drawDrawer(); } }, settle);
  else loadRelated();
  markSelected();
  drawDrawer();
  if (focus) {
    const title = slot.querySelector('[data-role="detail-title"]');
    if (title) title.focus({ preventScroll: true });
  }
}

function closeRow({ focus = true } = {}) {
  if (!detail) return;
  const index = detail.index;
  if (detailAbort) detailAbort.abort();
  clearTimeout(detailTimer);
  detail = null;
  if (!slot) return;
  drawDrawer();
  markSelected();
  if (!focus) return;
  // 焦点还给刚才那一行：用键盘的人关掉详情后接着往下看，不用从头再找
  const row = slot.querySelector(`tr[data-action="open-row"][data-index="${index}"]`);
  if (row) row.focus({ preventScroll: true });
}

/* ─── 路由进来 ─────────────────────────────────────────────────── */

/* 打开一张表（或者同一张表换了一组条件）。changed：是不是换了一张表。 */
export function openTable(container, database, table, queryString, { changed = false, reload = false } = {}) {
  slot = container;
  const next = parseView(queryString);
  const sameView = !changed && formatView(next) === formatView(view);
  route = { database, table };
  if (changed || reload) {
    preview = { status: 'idle', data: null, error: null };
    if (controller) controller.abort();
    if (detailAbort) detailAbort.abort();
    detail = null;
  }
  if (changed) {
    draft = { column: '', op: '' };
    panel = '';
    hidden = new Set();
    showEmpty = false;
    expanded = false;
    lastProfile = null;
  }
  view = next;
  refreshProfiles();
  drawTable();
  if (view.tab === 'records' && (!sameView || preview.status === 'idle')) loadPreview();
}

/* 离开表页（去了目录或别的视图）：还在路上的请求不用等了。 */
export function leaveTable() {
  if (controller) controller.abort();
  if (detailAbort) detailAbort.abort();
  if (profileAbort) profileAbort.abort();
  clearTimeout(searchTimer);
  clearTimeout(detailTimer);
  detail = null;
  slot = null;
}

/* 概况到了。是这张表的就把用得上它的地方重画一遍。 */
export function onProfile(entry) {
  if (!slot || !entry) return;
  // 记录详情里列着的那张表的配色到了
  if (detail && detail.tables.has(entry.database + '\u0000' + entry.table)) drawDrawer();
  if (entry.database !== route.database || entry.table !== route.table) return;
  drawInsights();
  if (entry !== baseEntry()) return;
  // 整张表的那一份还决定了：表头的记录数、哪些字段画成色点标签、哪些空字段收起来
  drawHeadMeta();
  if (view.tab === 'fields') { drawFields(); return; }
  drawTools();
  drawChips();
  if (panel) drawPanel();
  if (preview.data && preview.status !== 'loading') drawResults();
  if (detail) drawDrawer();
}

/* ─── 事件 ─────────────────────────────────────────────────────── */

export const tableActions = {
  tab: (target) => {
    view = { ...view, tab: target.dataset.tab };
    syncAddress();
    drawTable();
    if (view.tab === 'records' && preview.status === 'idle') loadPreview();
    $('tab-' + view.tab).focus();
  },
  sort: (target) => {
    const key = target.dataset.key;
    let sort = null;
    if (!view.sort || view.sort.key !== key) sort = { key, direction: 'asc' };
    else if (view.sort.direction === 'asc') sort = { key, direction: 'desc' };
    applyView({ sort, page: 1 });
  },
  page: (target) => applyView({ page: Math.max(1, Number(target.dataset.page) || 1) }),
  panel: (target) => {
    panel = panel === target.dataset.name ? '' : target.dataset.name;
    drawTools();
    drawPanel();
    if (panel === 'filter' && $('filterValue')) $('filterValue').focus();
  },
  'toggle-insights': () => {
    state.explorer = { ...(state.explorer || {}), insights: !insightsOn() };
    save();
    drawTools();
    drawInsights();
  },
  'toggle-expand': () => { expanded = !expanded; drawInsights(); },
  'retry-profile': () => { retryProfile(viewEntry(), { search: view.search, filters: view.filters }); drawInsights(); },
  facet: (target) => {
    const { column, value } = target.dataset;
    const active = view.filters.find((filter) => filter.column === column && filter.op === 'eq');
    replaceFilters(column, active && sameValue(active.value, value) ? [] : [{ column, op: 'eq', value }]);
  },
  period: (target) => {
    const { column, start, end } = target.dataset;
    const range = dateRange(column);
    replaceFilters(column, range && range.start === start && range.end === end ? [] : [
      { column, op: 'gte', value: start }, { column, op: 'lt', value: end },
    ]);
  },
  'remove-filter': (target) => {
    const filters = target.dataset.column !== undefined
      ? view.filters.filter((filter) => filter.column !== target.dataset.column)
      : view.filters.filter((_, position) => position !== Number(target.dataset.index));
    applyView({ filters, page: 1 });
  },
  'clear-search': () => { if ($('tableSearch')) $('tableSearch').value = ''; applyView({ search: '', page: 1 }); },
  'clear-filters': () => { if ($('tableSearch')) $('tableSearch').value = ''; applyView({ search: '', filters: [], page: 1 }); },
  'show-empty': () => { showEmpty = true; drawTools(); drawPanel(); drawChips(); drawResults(); },
  'show-all-columns': () => { hidden = new Set(); showEmpty = true; drawTools(); drawPanel(); drawChips(); drawResults(); },
  'retry-preview': () => loadPreview(),
  'open-row': (target, event) => {
    // 在单元格里拖选文字不算点开这一行
    if (window.getSelection && String(window.getSelection())) return;
    if (event.target.closest('a, button')) return;
    const index = Number(target.dataset.index);
    if (detail && detail.index === index) closeRow(); else openRow(index);
  },
  'close-row': () => closeRow(),
  'row-step': (target) => {
    if (detail) openRow(detail.index + (target.dataset.which === 'prev' ? -1 : 1), { focus: false, settle: STEP_DELAY_MS });
  },
  'retry-related': () => { if (detail) { detail.related = new Map(); loadRelated({ fresh: true }); drawDrawer(); } },
  'ask-about': () => {
    const table = currentTable();
    if (!table) return;
    setPrefill(t('data.askPrefill', { table: labelOf(table) }));
    location.hash = '#/ask';
  },
  'copy-sql': async () => {
    if (preview.data) toast(t(await copyText(preview.data.sql) ? 'toast.copied' : 'toast.copyFailed'));
  },
  'copy-page': async () => {
    const table = currentTable();
    if (!table || !preview.data || !preview.data.rows.length) return;
    const columns = gridColumns(table, preview.data);
    const text = toTsv(
      columns.map((column) => (column.unit ? `${column.label} (${column.unit})` : column.label)),
      preview.data.rows.map((row) => columns.map((column) => row[column.index])),
    );
    toast(t(await copyText(text) ? 'toast.tableCopied' : 'toast.copyFailed'));
  },
};

function commitSearch() {
  clearTimeout(searchTimer);
  const input = $('tableSearch');
  if (!input) return;
  const term = input.value.trim();
  if (term !== view.search) applyView({ search: term, page: 1 });
}

export function bindTable(root) {
  root.addEventListener('submit', (event) => {
    const form = event.target;
    if (form.dataset.form === 'search') {
      event.preventDefault();
      commitSearch();
    } else if (form.dataset.form === 'filter') {
      event.preventDefault();
      const column = $('filterColumn').value;
      const op = $('filterOp').value;
      const value = VALUELESS.includes(op) ? '' : $('filterValue').value.trim();
      if (!column || (!VALUELESS.includes(op) && !value)) {
        if ($('filterValue')) $('filterValue').focus();
        return;
      }
      if (view.filters.length >= maxFilters()) return;
      applyView({ filters: [...view.filters, { column, op, value }], page: 1 });
      if ($('filterValue')) $('filterValue').focus();
    }
  });

  root.addEventListener('input', (event) => {
    // 边打边搜：停手一小会儿再发，不是每敲一个字发一次
    if (event.target.id !== 'tableSearch') return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(commitSearch, SEARCH_DELAY_MS);
  });

  root.addEventListener('change', (event) => {
    const kind = event.target.dataset.input;
    // 重画是因为可选的条件、输入提示跟着字段变。焦点留在刚才操作的那个下拉框上
    //（重画按 id 找回它）：用方向键挨个看选项时，每按一下都会触发一次 change。
    if (kind === 'filter-column') { draft = { column: event.target.value, op: '' }; drawPanel(); }
    if (kind === 'filter-op') {
      const typed = $('filterValue') ? $('filterValue').value : '';
      draft = { ...draft, column: $('filterColumn').value, op: event.target.value };
      drawPanel();
      if ($('filterValue')) $('filterValue').value = typed;
    }
    if (kind === 'page-size') applyView({ pageSize: Number(event.target.value) || PAGE_SIZES[0], page: 1 });
    if (kind === 'column-toggle') {
      const name = event.target.dataset.name;
      if (event.target.checked) {
        hidden.delete(name);
        // 勾上一个空字段，就是想看空字段了
        if (emptyColumns().includes(name)) showEmpty = true;
      } else {
        hidden.add(name);
      }
      drawTools();
      drawPanel();
      drawChips();
      drawResults();
    }
  });

  // 关掉记录详情：焦点不一定在数据页里面（点过页面空白处就在 body 上），所以挂在整个文档上
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !slot || !detail || root.hidden) return;
    if (document.querySelector('dialog[open]')) return;
    event.preventDefault();
    closeRow();
  });

  root.addEventListener('keydown', (event) => {
    if (!slot) return;
    // 标签页：左右方向键切换（roving tabindex）
    if (event.target.matches('[role="tab"]') && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
      event.preventDefault();
      tableActions.tab({ dataset: { tab: view.tab === 'records' ? 'fields' : 'records' } });
      return;
    }
    const row = event.target.matches('tr[data-action="open-row"]') ? event.target : null;
    if (!row) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openRow(Number(row.dataset.index));
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const next = event.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling;
      if (next) { event.preventDefault(); next.focus(); }
    }
  });
}
