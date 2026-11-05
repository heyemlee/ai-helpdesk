/* 表格：问答结果与数据预览共用一套画法。

   列头第一行是给人看的名字（带单位），第二行是技术字段名——
   员工看前者，学习和排查时对后者；两者都在，不用切换模式。 */

import { html } from './dom.js';
import { formatCell } from './format.js';
import { t } from './i18n.js';

/* 一个取值前面带色点的小标签。颜色只是帮眼睛扫，认的还是字。 */
export const valuePill = (text, slot) =>
  html`<span class="pill"><span class="viz-dot viz-s${slot}" aria-hidden="true"></span>${text}</span>`;

/* columns: [{ key, label, unit, numeric, plain, hint }]，rows: 与 columns 对齐的二维数组。
   sort: { key, direction } | null；sortAction 给了才画成可点的列头。

   数据浏览多用的几样（问答结果不传，画出来和以前一样）：
   - column.index：这一列在行数组里的位置。收起了几列之后，列的顺序和行里的位置就对不上了；
   - column.slots：取值 → 第几个分类色（Map），有的话这一列画成带色点的小标签；
   - column.clip：长内容截成一行（完整内容在悬停提示和记录详情里），不撑高整行；
   - rowAction：给了之后每一行可点、可用键盘聚焦，selected 是当前打开的那一行。 */
export function renderTable({
  columns, rows, sort = null, sortAction = '', scope = '', label = '',
  rowAction = '', selected = -1,
}) {
  const head = columns.map((column) => {
    const active = sort && sort.key === column.key;
    const ariaSort = active ? (sort.direction === 'desc' ? 'descending' : 'ascending') : 'none';
    const title = html`${column.label}${column.unit
      ? html` <span class="th-unit">(${column.unit})</span>` : ''}`;
    const inner = sortAction && column.sortable !== false
      ? html`<button type="button" class="th-btn" data-action="${sortAction}" data-scope="${scope}"
            data-key="${column.key}" title="${t('table.sortBy', { column: column.label })}">${title}<span
            class="sort-mark" aria-hidden="true">${active ? (sort.direction === 'desc' ? '▼' : '▲') : ''}</span></button>`
      : title;
    return html`<th scope="col" class="${column.numeric ? 'num' : ''}"
        ${sortAction && column.sortable !== false ? html`aria-sort="${ariaSort}"` : ''}
        ${column.hint ? html`title="${column.hint}"` : ''}>${inner}${column.label !== column.key
      ? html`<span class="th-sub">${column.key}</span>` : ''}</th>`;
  });

  const cells = (row) => columns.map((column, position) => {
    const value = row[column.index ?? position];
    const cell = formatCell(value, { unit: column.unit, plain: column.plain, kind: column.numeric ? 'number' : '' });
    const clipped = column.clip && cell.text.length > 36;
    const classes = [
      column.numeric || cell.numeric ? 'num' : '',
      cell.empty ? 'empty-cell' : '',
      column.clip ? 'clip' : cell.text.length > 80 ? 'wrap' : '',
    ].filter(Boolean).join(' ');
    const slot = column.slots && !cell.empty ? column.slots.get(String(value)) : 0;
    return html`<td class="${classes}" ${clipped ? html`title="${cell.text}"` : ''}>${cell.empty
      ? html`<span aria-label="${t('table.empty')}">—</span>`
      : slot ? valuePill(cell.text, slot) : cell.text}</td>`;
  });

  const body = rows.map((row, index) => (rowAction
    ? html`<tr class="is-clickable" tabindex="0" data-action="${rowAction}" data-index="${index}"
        ${index === selected ? html`aria-current="true"` : ''}>${cells(row)}</tr>`
    : html`<tr>${cells(row)}</tr>`));

  return html`<div class="table-wrap${rowAction ? ' is-browse' : ''}" tabindex="0" role="region" aria-label="${label}">
    <table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
  </div>`;
}

const NUMERIC_TEXT = /^-?\d+(\.\d+)?$/;
const INTEGER_TEXT = /^-?\d+$/;

/* 两个值按数字比的结果；有一个不是数字就返回 null。

   数字不一定是 number 类型：PostgreSQL 的 NUMERIC、超过 2^53 的整数到页面上都是字符串
   （服务端特意这样给，免得精度丢了）。把它们按字符比的话，-0.50、-9.00、-10.00
   就是「升序」。超出双精度的长整数用 BigInt 比，不经过 Number。 */
function compareNumeric(left, right) {
  const numeric = (value) => typeof value === 'number' || (typeof value === 'string' && NUMERIC_TEXT.test(value));
  if (!numeric(left) || !numeric(right)) return null;
  const long = (value) => typeof value === 'string' && INTEGER_TEXT.test(value) && value.length > 15;
  if (long(left) || long(right)) {
    try {
      const a = BigInt(typeof left === 'number' ? Math.trunc(left) : left.split('.')[0]);
      const b = BigInt(typeof right === 'number' ? Math.trunc(right) : right.split('.')[0]);
      if (a !== b) return a < b ? -1 : 1;
    } catch (_) { /* 落到下面按 Number 比 */ }
  }
  return Number(left) - Number(right);
}

/* 排序只动显示顺序。空值永远排在最后，升序降序都一样——
   「没有值」不是「最小的值」。 */
export function sortRows(rows, index, direction) {
  const factor = direction === 'desc' ? -1 : 1;
  const isEmpty = (value) => value === null || value === undefined || value === '';
  return [...rows].sort((a, b) => {
    const left = a[index];
    const right = b[index];
    if (isEmpty(left) || isEmpty(right)) return isEmpty(left) - isEmpty(right);
    const numeric = compareNumeric(left, right);
    if (numeric !== null) return numeric * factor;
    return String(left).localeCompare(String(right), undefined, { numeric: true }) * factor;
  });
}

export function renderPager({ page, pageCount, action, scope = '' }) {
  if (pageCount <= 1) return '';
  return html`<span class="pager">
    <button type="button" class="btn btn-sm" data-action="${action}" data-scope="${scope}" data-which="prev" data-page="${page - 1}"
      ${page <= 1 ? 'disabled' : ''}>${t('table.prev')}</button>
    <span>${t('table.pageOf', { page, pages: pageCount })}</span>
    <button type="button" class="btn btn-sm" data-action="${action}" data-scope="${scope}" data-which="next" data-page="${page + 1}"
      ${page >= pageCount ? 'disabled' : ''}>${t('table.next')}</button>
  </span>`;
}
