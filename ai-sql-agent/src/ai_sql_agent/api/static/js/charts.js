/* 统计图的几种画法：占比条、取值清单、按月的柱子、填充率。都是 html`` 片段，不带状态。

   几条规矩，写在这里是因为它们决定了下面每个函数长什么样：

   - 颜色只干一件事。分类色表示「是哪一个取值」，按字段取值的固定顺序分配，筛选前后
     同一个取值永远是同一个颜色；取值超过 MAX_SERIES 个就不上色，改成单色的条形——
     再多的颜色互相分不清。配色的顺序在样式表里，是校验过色弱可辨的那一组。
   - 颜色从不单独出现：每个色块旁边都有名字和数字，那份清单本身就是这张图的数据表。
   - 条和柱都细，相邻色块之间留 2px 的底色做间隔，不描边。
   - 文字用正文的颜色，不用数据的颜色。 */

import { html } from './dom.js';
import { formatCount, formatShare } from './format.js';
import { t } from './i18n.js';

export const MAX_SERIES = 6;
const MAX_BARS = 36;

/* 取值 → 第几个颜色（1 起）。values 是这个字段**没有筛选时**的完整取值清单，
   顺序由服务端定（声明过的按声明顺序）。取不全或者太多的不上色，返回空表。 */
export function seriesSlots(facet) {
  const slots = new Map();
  if (!facet || !facet.complete || facet.values.length > MAX_SERIES) return slots;
  // 没声明顺序的按取值本身排，不按数量排：数量会变，颜色不能跟着换
  const values = facet.ordered_by === 'declared'
    ? facet.values
    : [...facet.values].sort((a, b) => String(a.value).localeCompare(String(b.value), undefined, { numeric: true }));
  values.forEach((item, index) => slots.set(String(item.value), index + 1));
  return slots;
}

const summary = (items) => items
  .filter((item) => item.count > 0)
  .map((item) => `${item.text} ${formatCount(item.count)}`)
  .join(' · ');

/* 占比条。items: [{ text, count, slot, muted }]。 */
export function stackedBar(items, label) {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  if (!total) return html`<div class="viz-stack is-empty" role="img" aria-label="${label}"></div>`;
  return html`<div class="viz-stack" role="img" aria-label="${label}: ${summary(items)}">${items
    .filter((item) => item.count > 0)
    .map((item) => html`<span class="viz-seg viz-s${item.slot}${item.muted ? ' is-muted' : ''}"
      style="flex-grow:${item.count}"
      title="${item.text} · ${formatCount(item.count)} · ${formatShare(item.count, total)}"></span>`)}</div>`;
}

/* 取值清单：一行一个取值，色点（或条形）+ 名字 + 数量 + 占比。
   给了 action 就是可点的——点一下按这个取值筛选，再点一下取消。

   items: [{ value, text, count, slot, active, truncated }]
   bars:  true 时每行画一条和数量成正比的单色条形（取值多、不上色的字段用这个）。 */
export function valueList({ column, items, total, action = '', bars = false }) {
  const peak = Math.max(1, ...items.map((item) => item.count));
  const anyActive = items.some((item) => item.active);
  return html`<ul class="viz-list${bars ? ' has-bars' : ''}">${items.map((item) => {
    const share = formatShare(item.count, total);
    const body = html`${bars ? '' : html`<span class="viz-dot${item.slot ? ` viz-s${item.slot}` : ''}" aria-hidden="true"></span>`}
      <span class="viz-name">${item.text}</span>
      ${bars ? html`<span class="viz-track" aria-hidden="true"><i style="width:${Math.max(item.count ? 2 : 0, (item.count / peak) * 100)}%"></i></span>` : ''}
      <span class="viz-count">${formatCount(item.count)}</span>
      <span class="viz-share">${share}</span>`;
    const title = `${item.text} · ${formatCount(item.count)}${share ? ' · ' + share : ''}`;
    // 截过的取值已经不是库里的原值，拿去做「等于」筛选一条也匹配不上：只显示，不让点
    if (!action || item.truncated) {
      return html`<li><span class="viz-row is-static" title="${title}">${body}</span></li>`;
    }
    return html`<li><button type="button" class="viz-row${anyActive && !item.active ? ' is-off' : ''}"
      data-action="${action}" data-column="${column}" data-value="${item.value}"
      aria-pressed="${String(Boolean(item.active))}" title="${title}">${body}</button></li>`;
  })}</ul>`;
}

const pad = (number) => String(number).padStart(2, '0');
const monthIndex = (period) => Number(period.slice(0, 4)) * 12 + Number(period.slice(5, 7)) - 1;
const monthStart = (index) => `${Math.floor(index / 12)}-${pad((index % 12) + 1)}-01`;

/* 按月的记录数 → 画得下的柱子。

   月份之间的空档补成 0（没有记录的月份也是一个月，不能把两头直接拼在一起）；
   月份太多时并成季度，还多就并成年。每根柱子带着它覆盖的日期范围
   [start, end)，点它的时候就是拿这两个日期去筛选。 */
export function rebucket(buckets) {
  const counts = new Map();
  (buckets || []).forEach((bucket) => {
    if (/^\d{4}-(0[1-9]|1[0-2])$/.test(bucket.period)) counts.set(monthIndex(bucket.period), bucket.count);
  });
  if (!counts.size) return { grain: 'month', bars: [] };

  const first = Math.min(...counts.keys());
  const last = Math.max(...counts.keys());
  const months = last - first + 1;
  const span = months <= MAX_BARS ? 1 : Math.ceil(months / 3) <= MAX_BARS + 4 ? 3 : 12;
  const grain = span === 1 ? 'month' : span === 3 ? 'quarter' : 'year';

  const bars = [];
  // 季度和年按日历对齐（一季度从一月起），不从数据的第一个月起算
  for (let start = first - (first % span); start <= last; start += span) {
    let count = 0;
    for (let index = start; index < start + span; index += 1) count += counts.get(index) || 0;
    const year = Math.floor(start / 12);
    const label = span === 1 ? `${year}-${pad((start % 12) + 1)}`
      : span === 3 ? `${year} Q${Math.floor((start % 12) / 3) + 1}` : String(year);
    bars.push({ label, start: monthStart(start), end: monthStart(start + span), count });
  }
  return { grain, bars };
}

/* 按月（季、年）的柱子。给了 action 就是可点的：点一根柱子按它的日期范围筛选。
   selected: { start, end } | null —— 当前按这个字段筛着的范围，范围外的柱子退后。 */
export function columnChart({ column, bars, label, action = '', selected = null }) {
  const peak = Math.max(1, ...bars.map((bar) => bar.count));
  const inside = (bar) => selected && bar.start >= selected.start && bar.end <= selected.end;
  const marks = bars.map((bar, index) => {
    const height = bar.count ? Math.max(3, (bar.count / peak) * 100) : 0;
    const tip = `${bar.label} · ${formatCount(bar.count)}`;
    const edge = index < bars.length / 4 ? ' tip-start' : index >= (bars.length * 3) / 4 ? ' tip-end' : '';
    const state = selected ? (inside(bar) ? ' is-on' : ' is-off') : '';
    const mark = html`<i style="height:${height}%"></i>`;
    if (!action) return html`<span class="viz-col${state}" title="${tip}">${mark}</span>`;
    return html`<button type="button" class="viz-col${state}${edge}" data-action="${action}" data-column="${column}"
      data-start="${bar.start}" data-end="${bar.end}" aria-pressed="${String(Boolean(inside(bar)))}"
      aria-label="${t('viz.barLabel', { label: bar.label, n: formatCount(bar.count) })}" data-tip="${tip}">${mark}</button>`;
  });
  return html`<div class="viz-cols${action ? '' : ' is-static'}" role="${action ? 'group' : 'img'}" aria-label="${label}">${marks}</div>
    <div class="viz-axis" aria-hidden="true"><span>${bars.length ? bars[0].label : ''}</span><span>${bars.length > 1 ? bars[bars.length - 1].label : ''}</span></div>`;
}

/* 填充率：这个字段有多少条记录填了值。轨道是同一个色系的浅色。 */
export function meter(part, whole, label) {
  const share = whole > 0 ? Math.min(1, Math.max(0, part / whole)) : 0;
  return html`<span class="viz-meter" role="img" aria-label="${label}"><i style="width:${share * 100}%"></i></span>`;
}

/* 一行里和数量成正比的条形（目录页上各张表的记录数）。 */
export function barRow(count, peak) {
  const width = peak > 0 && count > 0 ? Math.max(2, (count / peak) * 100) : 0;
  return html`<span class="viz-track" aria-hidden="true"><i style="width:${width}%"></i></span>`;
}
