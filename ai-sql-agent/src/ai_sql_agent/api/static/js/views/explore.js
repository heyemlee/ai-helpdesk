/* 数据浏览：不提问，先看看系统里有什么。

   三层，从粗到细：
   - 全部数据（#/data）：有哪几个库、各有多少张表多少条记录、库与库之间靠什么连着；
   - 一个库（#/data/<库>）：它的每张表长什么样——多少条、状态怎么分布、记录落在哪段时间；
   - 一张表（#/data/<库>/<表>）：概览 + 记录 + 字段说明，见 exploreTable.js。

   左边的导航一直在：从任何一张表到另一个库的任何一张表都只要点一下。

   目录来自 /catalog，统计来自 /catalog/{库}/{表}/profile——两个都是服务端按当前身份
   算出来的。这个文件里没有任何表名、字段名或样例数据：服务端没给的，这里画不出来。 */

import {
  catalog, domainTitle, domains, ensureColumns, findDomain, loadCatalog,
} from '../catalog.js';
import { barRow, columnChart, rebucket, seriesSlots, stackedBar, valueList } from '../charts.js';
import { dataHref } from '../dataRoute.js';
import { $, delegate, html, icon, setHtml } from '../dom.js';
import { formatCompact, formatCount, formatRange, formatTime, labelOf, localized } from '../format.js';
import { t, tn } from '../i18n.js';
import { buildLinks, crossDatabaseLinks } from '../links.js';
import { findProfile, profileSupport } from '../profile.js';
import { emit, on, state } from '../state.js';
import { notice } from '../steps.js';
import { bindTable, drawTable, leaveTable, onProfile, openTable, tableActions } from './exploreTable.js';
import {
  allTables, catalogProblem, consumePrefill, countChip, databaseSlot, dbTile, domainSummary, fieldCount,
  requestBreakdowns, requestCounts, rowCount, setPrefill, sumCounts, tableSummary, unavailableReason,
} from './exploreParts.js';

// 一个库里的表不超过这么多时，库页面上每张表都画分布和时间轴；再多就只给行数
const RICH_CARDS_UP_TO = 12;

let root;
let route = { database: '', table: '' };
let stale = false;            // 这一页不在眼前时目录变了（换了身份、重连）：回来要重读
let needle = '';              // 导航里的查找词
let navOpen = false;          // 窄屏上导航是不是展开着
let frame = 0;

const pageSlot = () => root.querySelector('[data-role="page"]');
const hashQuery = () => { const mark = location.hash.indexOf('?'); return mark === -1 ? '' : location.hash.slice(mark + 1); };
const matches = (term, texts) => texts.some((text) => String(text || '').toLowerCase().includes(term));

/* ─── 左边的导航 ───────────────────────────────────────────────── */

function tree() {
  const term = needle.trim().toLowerCase();
  const groups = domains().map((domain) => {
    const domainHit = !term || matches(term, [domainTitle(domain), domain.database, ...(domain.aliases || [])]);
    const tables = domain.tables.map((table) => {
      const tableHit = !term || matches(term, [labelOf(table), table.name, ...(table.aliases || [])]);
      // 表名对不上时再看字段：找「手机号在哪张表」的人打的是字段名
      const column = tableHit ? null : (table.columns || [])
        .find((item) => matches(term, [labelOf(item), item.name, ...(item.aliases || [])]));
      return { table, column, show: domainHit || tableHit || Boolean(column) };
    }).filter((item) => item.show);
    return { domain, tables, show: domainHit || tables.length > 0 };
  }).filter((group) => group.show);

  if (!groups.length) return html`<p class="muted dx-nav-empty">${t('data.nav.noMatch')}</p>`;
  return html`<ul class="dx-tree">${groups.map(({ domain, tables }) => {
    const here = route.database === domain.database;
    return html`<li>
      <a class="dx-db-link" id="nav:${domain.database}" href="${dataHref(domain.database)}"
        ${here && !route.table ? html`aria-current="page"` : ''}>
        ${dbTile(domain.database, 'is-sm')}<span class="dx-nav-name">${domainTitle(domain)}</span>
        ${domain.status !== 'available' ? html`<span class="chip chip-danger">${t('data.unavailable')}</span>` : ''}</a>
      ${tables.length ? html`<ul>${tables.map(({ table, column }) => html`<li>
        <a class="dx-table-link" id="nav:${domain.database}:${table.name}" href="${dataHref(domain.database, table.name)}"
          ${here && route.table === table.name ? html`aria-current="page"` : ''}>
          <span class="dx-nav-name">${labelOf(table)}${column
            ? html`<span class="dx-nav-hint">${t('data.nav.hasField', { name: labelOf(column) })}</span>` : ''}</span>
          ${table.status === 'missing' ? html`<span class="chip chip-warn">${t('data.missing')}</span>` : countChip(domain.database, table.name)}
        </a></li>`)}</ul>` : ''}
    </li>`;
  })}</ul>`;
}

function drawTree() {
  const node = root.querySelector('[data-role="tree"]');
  if (node) setHtml(node, tree());
}

function drawSide() {
  const side = root.querySelector('[data-role="side"]');
  side.hidden = catalog.status !== 'ready' || !domains().length;
  // 没有导航时内容区占满整行（样式按这个标记切换栏数）
  root.querySelector('.dx').dataset.side = side.hidden ? 'off' : 'on';
  if (side.hidden) { setHtml(side, ''); return; }
  setHtml(side, html`<button type="button" class="dx-side-toggle" data-action="toggle-nav" aria-expanded="${String(navOpen)}">
      ${icon('data')}<span class="grow">${t('data.nav.browse')}</span>${icon('chevronDown', 'icon-sm')}</button>
    <div class="dx-side-body" data-open="${String(navOpen)}">
      <label class="dx-find">${icon('search', 'icon-sm')}<span class="sr-only">${t('data.nav.find')}</span>
        <input class="input" id="navFind" type="search" maxlength="60" value="${needle}"
          placeholder="${t('data.nav.find')}" autocomplete="off"></label>
      <a class="dx-home" id="nav:" href="#/data" ${!route.database ? html`aria-current="page"` : ''}>${icon('home', 'icon-sm')}${t('data.nav.all')}</a>
      <nav aria-label="${t('data.nav.label')}" data-role="tree">${tree()}</nav>
    </div>`);
}

/* ─── 全部数据 ─────────────────────────────────────────────────── */

function kpi(label, value, sub = '', title = '') {
  return html`<div class="kpi"><p class="kpi-label">${label}</p>
    <p class="kpi-value" ${title ? html`title="${title}"` : ''}>${value}</p>
    ${sub ? html`<p class="kpi-sub muted">${sub}</p>` : ''}</div>`;
}

/* 记录数这一格。还没数完时先给占位；有表没数出来时照实说「至少」。 */
function recordKpi(pairs, label) {
  if (profileSupport() !== 'on') return '';
  const counts = sumCounts(pairs);
  // 表太多、没有逐张去数的时候不给总数：给一个只加了一部分的数，比不给更糟
  if (!counts.expected || counts.unasked) return '';
  if (counts.known + counts.failed < counts.expected) return kpi(label, '···', t('data.kpi.counting'));
  const short = formatCompact(counts.total);
  const full = formatCount(counts.total);
  // 大数写短了才在下面补一行完整的；本来就没缩写时不重复一遍
  const sub = counts.failed ? tn('data.kpi.partial', counts.failed) : short === full ? '' : t('data.kpi.exact', { n: full });
  return kpi(label, (counts.failed ? '≥ ' : '') + short, sub, full);
}

function tableRows(domain, peak) {
  return html`<ul class="tbl-rows">${domain.tables.map((table) => {
    const count = rowCount(domain.database, table.name);
    return html`<li><a class="tbl-row" href="${dataHref(domain.database, table.name)}">
      <span class="tbl-name">${labelOf(table)} <span class="tech">${table.name}</span></span>
      ${table.status === 'missing'
        ? html`<span class="chip chip-warn">${t('data.missing')}</span>`
        : html`${barRow(count || 0, peak)}${countChip(domain.database, table.name)}`}
    </a></li>`;
  })}</ul>`;
}

function databaseCard(domain, peak) {
  const unavailable = domain.status !== 'available';
  const pairs = domain.tables.map((table) => ({ domain, table }));
  const counts = sumCounts(pairs);
  const facts = [tn('data.tableCount', domain.tables.length)];
  if (profileSupport() === 'on' && counts.expected && counts.known === counts.expected) {
    facts.push(tn('data.recordCount', counts.total, { count: formatCount(counts.total) }));
  }
  return html`<section class="db-card db-c${databaseSlot(domain.database)}" aria-labelledby="db-${domain.database}">
    <header class="db-card-head">
      ${dbTile(domain.database, 'is-lg')}
      <div class="grow">
        <h2 class="db-card-title" id="db-${domain.database}"><a href="${dataHref(domain.database)}">${domainTitle(domain)}</a></h2>
        <p class="tech">${domain.database}${unavailable ? '' : ' · ' + facts.join(' · ')}</p>
      </div>
      ${unavailable ? html`<span class="chip chip-danger">${icon('error', 'icon-sm')}${t('data.unavailable')}</span>` : ''}
    </header>
    <p class="db-card-desc">${domainSummary(domain)}</p>
    ${unavailable
      ? notice('danger', 'error', t('data.unavailable.title'), html`<p>${unavailableReason(domain)}</p><p>${t('data.unavailable.body')}</p>`)
      : domain.tables.length ? tableRows(domain, peak) : html`<p class="muted">${t('data.noTables')}</p>`}
  </section>`;
}

/* 库与库靠什么连着：一行一个「共用的键」，一列一个库，格子里是用到这个键的表。 */
function keyMatrix(links, list) {
  if (!links.groups.length) return '';
  return html`<section class="card card-pad stack" aria-labelledby="linksTitle">
    <div><h2 class="section-title" id="linksTitle">${t('data.links.title')}</h2>
      <p class="muted">${t('data.links.lead')}</p></div>
    <div class="matrix-wrap"><table class="key-matrix">
      <thead><tr><th scope="col">${t('data.links.key')}</th>${list.map((domain) => html`<th scope="col">
        <a class="matrix-db" href="${dataHref(domain.database)}">${dbTile(domain.database, 'is-xs')}${domainTitle(domain)}</a></th>`)}</tr></thead>
      <tbody>${links.groups.map((group) => {
        const hubLabel = labelOf(group.hub.columnEntry);
        return html`<tr>
          <th scope="row"><span class="key-name">${icon('key', 'icon-sm')}${hubLabel}</span>
            <span class="tech">${group.hub.column}</span>
            <span class="muted key-span">${group.databases.length > 1
              ? t('data.links.span', { tables: group.members.length, databases: group.databases.length })
              : t('data.links.spanSame', { tables: group.members.length })}</span></th>
          ${list.map((domain) => {
            const members = group.members.filter((member) => member.database === domain.database);
            return html`<td>${members.map((member) => {
              const own = labelOf(member.columnEntry);
              return html`<a class="chip chip-link" href="${dataHref(member.database, member.table)}">${labelOf(member.tableEntry)}${own !== hubLabel
                ? html`<span class="muted"> · ${own}</span>` : ''}</a>`;
            })}</td>`;
          })}
        </tr>`;
      })}</tbody>
    </table></div>
  </section>`;
}

function glossary(data) {
  if (!data.glossary || !data.glossary.length) return '';
  return html`<details class="disclosure">
    <summary>${t('data.glossary', { n: data.glossary.length })}</summary>
    <div class="disclosure-body"><p class="muted">${t('data.glossaryNote')}</p>
      <dl class="glossary">${data.glossary.map((item) => {
        const term = localized(item, 'term');
        const definition = localized(item, 'definition');
        return html`<div><dt ${term.original ? html`lang="zh"` : ''}>${term.text}</dt>
          <dd ${definition.original ? html`lang="zh" class="original-note" title="${t('data.originalNote')}"` : ''}>${definition.text}</dd></div>`;
      })}</dl></div>
  </details>`;
}

function overviewPage() {
  const problem = catalogProblem();
  const data = catalog.data;
  const list = domains();
  const pairs = allTables();
  const fields = pairs.reduce((sum, { table }) => sum + fieldCount(table), 0);
  // 所有条形共用一把尺：最长的那条是全部表里记录最多的一张
  const peak = Math.max(0, ...pairs.map(({ domain, table }) => rowCount(domain.database, table.name) || 0));
  return html`<div class="page page-wide stack-lg">
    <div class="page-head">
      <div><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>
        <p class="page-sub">${t('data.lead')}</p></div>
      ${data ? html`<div class="row"><span class="muted">${t('data.readAt', { time: formatTime(data.generated_at) })}</span>
        <button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.refresh')}</button></div>` : ''}
    </div>
    ${problem}
    ${data ? html`
      ${data.access_limited ? notice('info', 'info', t('data.limited.title'), html`<p>${t('data.limited.body')}</p>`) : ''}
      ${list.length ? html`
        <div class="kpis">
          ${kpi(t('data.kpi.databases'), formatCount(list.length))}
          ${kpi(t('data.kpi.tables'), formatCount(pairs.length))}
          ${kpi(t('data.kpi.fields'), formatCount(fields))}
          ${recordKpi(pairs, t('data.kpi.records'))}
        </div>
        <div class="db-grid">${list.map((domain) => databaseCard(domain, peak))}</div>
        ${keyMatrix(buildLinks(data.domains), list)}`
        : html`<div class="empty-state"><strong>${t('data.empty.title')}</strong>${t('data.empty.body')}</div>`}
      ${glossary(data)}
      ${state.learning ? html`<section class="learn"><div class="learn-head"><span class="row">${icon('learn')}${t('learn.catalog.title')}</span></div>
        <div class="learn-body"><p class="learn-note">${t('learn.catalog.note')}</p>
        <p class="learn-src">src/ai_sql_agent/catalog/service.py · api/catalog.py · semantic/loader.py · retrieval/access.py</p></div></section>` : ''}
    ` : ''}
  </div>`;
}

/* ─── 一个库 ───────────────────────────────────────────────────── */

/* 表卡片上的那张小图：先找声明过取值的分布（状态、类型），没有就找最常见的几个取值。只看不点。 */
function headline(profile, nameOf) {
  const rows = profile.total_rows || 0;
  const live = profile.facets.filter((facet) => facet.values.some((item) => item.count > 0));
  const series = live.find((facet) => seriesSlots(facet).size);
  if (series) {
    const slots = seriesSlots(series);
    const items = series.values.map((item) => ({
      value: item.value, text: String(item.value), count: item.count, slot: slots.get(String(item.value)),
    }));
    const total = items.reduce((sum, item) => sum + item.count, 0);
    return { column: series.column, body: html`${stackedBar(items, nameOf(series.column))}${valueList({ column: series.column, items, total })}` };
  }
  const most = (facet) => Math.max(...facet.values.map((item) => item.count));
  const top = live.find((facet) => facet.complete || most(facet) >= Math.max(2, rows * 0.005));
  if (!top) return null;
  const items = [...top.values].sort((a, b) => b.count - a.count).slice(0, 4)
    .map((item) => ({ value: item.value, text: String(item.value), count: item.count, slot: 0 }));
  return { column: top.column, body: valueList({ column: top.column, items, total: rows, bars: true }) };
}

function tableCard(domain, table, rich) {
  // 只看已经要过的那一份；要不要去取由 showData 决定（表多的库不逐张取分布）
  const entry = profileSupport() === 'on' ? findProfile(domain.database, table.name, { breakdowns: rich }) : null;
  const profile = entry && entry.status === 'ready' ? entry.data : null;
  const labels = new Map((table.columns || []).map((column) => [column.name, labelOf(column)]));
  const head = profile && rich ? headline(profile, (name) => labels.get(name) || name) : null;
  const line = profile && rich ? profile.timelines.find((item) => item.buckets.length) : null;
  const span = profile ? profile.columns.find((column) => line && column.name === line.column && column.min !== undefined) : null;
  return html`<article class="tbl-card">
    <header class="tbl-card-head">
      <div class="grow"><h3 class="tbl-card-title"><a class="stretched" href="${dataHref(domain.database, table.name)}">${labelOf(table)}</a></h3>
        <p class="tech">${table.name} · ${tn('data.fieldCount', fieldCount(table))}</p></div>
      ${table.status === 'missing' ? html`<span class="chip chip-warn">${t('data.missing')}</span>`
        : profile && typeof profile.total_rows === 'number'
          ? html`<p class="tbl-card-count" title="${formatCount(profile.total_rows)}">${formatCompact(profile.total_rows)}<span>${t('data.recordsUnit')}</span></p>`
          : entry && entry.status === 'loading' ? html`<span class="spinner" aria-hidden="true"></span>` : ''}
    </header>
    <p class="tbl-card-desc">${tableSummary(table)}</p>
    ${head ? html`<div class="tbl-card-viz"><p class="viz-caption">${labels.get(head.column) || head.column}</p>${head.body}</div>` : ''}
    ${line ? html`<div class="tbl-card-viz"><p class="viz-caption">${labels.get(line.column) || line.column}${span
      ? html`<span class="muted"> · ${formatRange({ min: String(span.min).slice(0, 10), max: String(span.max).slice(0, 10) })}</span>` : ''}</p>
      ${columnChart({ column: line.column, bars: rebucket(line.buckets).bars, label: t('viz.timelineLabel', { column: labels.get(line.column) || line.column }) })}</div>` : ''}
  </article>`;
}

function databaseLinks(domain) {
  const groups = crossDatabaseLinks(buildLinks(catalog.data.domains), domain.database);
  if (!groups.length) return '';
  return html`<section class="card card-pad stack" aria-labelledby="dbLinksTitle">
    <div><h2 class="section-title" id="dbLinksTitle">${t('data.links.fromHere')}</h2>
      <p class="muted">${t('data.links.fromHereLead')}</p></div>
    <ul class="link-list">${groups.map((group) => html`<li>
      <a class="link-db" href="${dataHref(group.domain.database)}">${dbTile(group.domain.database, 'is-sm')}${domainTitle(group.domain)}</a>
      <ul>${group.links.map(({ mine, other }) => html`<li>
        <a href="${dataHref(mine.database, mine.table)}">${labelOf(mine.tableEntry)}</a>
        <span class="muted">${labelOf(mine.columnEntry)}</span>
        <span class="link-arrow" aria-hidden="true">↔</span><span class="sr-only">${t('data.links.matches')}</span>
        <a href="${dataHref(other.database, other.table)}">${labelOf(other.tableEntry)}</a>
        <span class="muted">${labelOf(other.columnEntry)}</span></li>`)}</ul>
    </li>`)}</ul>
  </section>`;
}

function databasePage() {
  const problem = catalogProblem();
  const domain = findDomain(route.database);
  if (problem || !catalog.data) {
    return html`<div class="page stack-lg"><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>${problem}</div>`;
  }
  if (!domain) {
    return html`<div class="page stack-lg">
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}"><a href="#/data">${t('data.title')}</a></nav>
      <h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.dbNotFound.title')}</h1>
      ${notice('info', 'info', t('data.dbNotFound.title'), html`<p>${t('data.dbNotFound.body')}</p>`)}
      <p><a class="btn" href="#/data">${icon('back', 'icon-sm')}${t('data.backToCatalog')}</a></p>
    </div>`;
  }

  const unavailable = domain.status !== 'available';
  const pairs = domain.tables.map((table) => ({ domain, table }));
  const fields = pairs.reduce((sum, { table }) => sum + fieldCount(table), 0);
  const linked = crossDatabaseLinks(buildLinks(catalog.data.domains), domain.database).length;
  const rich = domain.tables.length <= RICH_CARDS_UP_TO;
  return html`<div class="page page-wide stack-lg">
    <div>
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}">
        <a href="#/data">${t('data.title')}</a><span aria-hidden="true">›</span><span>${domainTitle(domain)}</span></nav>
      <div class="page-head">
        <div class="dx-title">${dbTile(domain.database, 'is-lg')}
          <div><h1 class="page-title" tabindex="-1" data-role="view-title">${domainTitle(domain)}</h1>
            <p class="tech">${domain.database} · ${domain.dialect} · ${t('data.readOnly')}</p></div></div>
        <div class="row"><button type="button" class="btn btn-sm" data-action="ask-database">${icon('ask', 'icon-sm')}${t('data.askAbout')}</button></div>
      </div>
      <p class="page-sub">${domainSummary(domain)}</p>
    </div>
    ${unavailable
      ? html`${notice('danger', 'error', t('data.unavailable.title'), html`<p>${unavailableReason(domain)}</p><p>${t('data.unavailable.body')}</p>`)}
        <div class="row"><button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button></div>`
      : html`<div class="kpis">
          ${kpi(t('data.kpi.tables'), formatCount(domain.tables.length))}
          ${kpi(t('data.kpi.fields'), formatCount(fields))}
          ${recordKpi(pairs, t('data.kpi.records'))}
          ${kpi(t('data.kpi.linked'), formatCount(linked))}
        </div>
        ${domain.tables.length
          ? html`<div class="tbl-grid">${domain.tables.map((table) => tableCard(domain, table, rich))}</div>`
          : html`<p class="muted">${t('data.noTables')}</p>`}
        ${databaseLinks(domain)}`}
  </div>`;
}

/* ─── 路由与重画 ───────────────────────────────────────────────── */

function drawPage() {
  if (route.table && catalog.status === 'ready') { drawTable(); return; }
  if (route.table) {
    // 目录还没到（或者没拿到）：先把原因摆出来，目录到了会再进来一次
    setHtml(pageSlot(), html`<div class="page stack-lg"><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>${catalogProblem()}</div>`);
    return;
  }
  setHtml(pageSlot(), route.database ? databasePage() : overviewPage());
}

/* 概况是一张表一张表到的。攒一小会儿一起画，不是到一张画一遍。
   用定时器，不用 requestAnimationFrame：页面不在前台（或者嵌在一个不重绘的容器里）时
   后者根本不触发，数字就会一直停在「正在统计」，切回来之前谁也不知道其实早就数完了。 */
function schedule() {
  if (frame) return;
  frame = setTimeout(() => {
    frame = 0;
    if (root.hidden) return;
    drawTree();
    if (!route.table) drawPage();
  }, 30);
}

/* 路由进来：#/data、#/data/<库> 或 #/data/<库>/<表>[?…] */
export async function showData(database = '', table = '', query = '', { reload = false } = {}) {
  const changed = route.database !== database || route.table !== table;
  const fresh = reload || stale;
  route = { database, table };
  stale = false;
  navOpen = false;
  if (catalog.status === 'idle') loadCatalog();
  drawSide();

  if (catalog.status === 'ready') {
    // 先把要的统计发出去，再画：这样第一遍画出来就知道哪些数字「还在数」
    requestCounts();
    if (database) requestCounts(database);
    const domain = database && !table ? findDomain(database) : null;
    if (domain && domain.tables.length <= RICH_CARDS_UP_TO) requestBreakdowns(domain);
  }
  if (!table || catalog.status !== 'ready') {
    if (!table) leaveTable();
    drawPage();
    return;
  }

  try { await ensureColumns(database, table); } catch (_) { /* 取不到字段说明时预览照常 */ }
  if (route.database !== database || route.table !== table) return;
  openTable(pageSlot(), database, table, query, { changed, reload: fresh });
}

export function initExplore() {
  root = $('view-data');
  setHtml(root, html`<div class="dx"><aside class="dx-side" data-role="side" hidden></aside><div class="dx-main" data-role="page"></div></div>`);
  bindTable(root);

  root.addEventListener('input', (event) => {
    if (event.target.id !== 'navFind') return;
    needle = event.target.value;
    drawTree();
  });

  delegate(root, 'click', {
    ...tableActions,
    reload: () => loadCatalog({ refresh: true }),
    'open-settings': () => emit('open-settings'),
    'toggle-nav': () => { navOpen = !navOpen; drawSide(); },
    'ask-database': () => {
      const domain = findDomain(route.database);
      if (!domain) return;
      setPrefill(t('data.askPrefill', { table: domainTitle(domain) }));
      location.hash = '#/ask';
    },
  });

  on('catalog', () => {
    if (root.hidden) { stale = true; return; }
    // 目录刷新后表可能变了：正在看的那一页跟着重读，不留一份对不上的旧数据
    showData(route.database, route.table, hashQuery(), { reload: true });
  });
  on('profile', (entry) => {
    if (root.hidden) return;
    onProfile(entry);
    schedule();
  });
  on('lang', () => { if (!root.hidden) { drawSide(); drawPage(); } });
  on('learning', () => { if (!root.hidden) drawPage(); });
}

/* 「就这份数据提问」带去问答页的话头；问答页显示出来之后取走。 */
export function takePrefill() {
  return consumePrefill();
}
