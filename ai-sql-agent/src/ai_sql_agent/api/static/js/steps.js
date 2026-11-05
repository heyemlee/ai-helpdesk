/* 结果卡片与查询过程共用的小件：一步结果的列、来源名、失败归类。 */

import { domainTitleOf, lookupColumn } from './catalog.js';
import { html, icon } from './dom.js';
import { descriptionOf, humanize, isNumericColumn, labelOf } from './format.js';
import { has, t } from './i18n.js';

/* 结果里的一列：名字、单位、是不是数字、数据字典里对应的条目。 */
export function stepColumns(step) {
  return step.columns.map((name) => {
    const match = lookupColumn(step.database, name, step.finalSql);
    const column = match ? match.column : null;
    return {
      key: name,
      label: column ? labelOf(column) : humanize(name),
      unit: step.units[name] || '',          // 只认服务端给的单位，不按列名猜
      numeric: isNumericColumn(step.rows, (row) => row[name]),
      plain: Boolean(column && (column.role === 'join_key' || column.primary_key)),
      hint: column ? descriptionOf(column).text : '',
      entry: column,
      table: match ? match.table : null,
    };
  });
}

export const stepMatrix = (step, columns) =>
  step.rows.map((row) => columns.map((column) => row[column.key]));

/* 「业务域的名字 (库标识符)」：业务名在前，技术库名跟在后面。 */
export function sourceLabel(database) {
  const title = domainTitleOf(database);
  return title === database ? database : `${title} (${database})`;
}

export const anomalyLabel = (code) => (has('anomaly.' + code) ? t('anomaly.' + code) : code);
export const anomalyHelp = (code) => (has('anomaly.' + code + '.help') ? t('anomaly.' + code + '.help') : '');

/* 服务端的报错原文是中文句子。页面按其中稳定的片段归成几种人能处理的情况，
   原文留在查询过程里。认不出的归为一般失败，不猜。 */
export function failureReason(error) {
  const text = String(error || '');
  if (text.includes('没有访问权限')) {
    const names = (text.split('没有访问权限：')[1] || '').split('。')[0].trim();
    return { key: 'fail.permission', detail: names };
  }
  if (text.includes('当前可见的表不足')) return { key: 'fail.schema', detail: '' };
  if (text.includes('超时')) return { key: 'fail.timeout', detail: '' };
  if (text.includes('授权上下文') || text.includes('无法解析')) return { key: 'fail.blocked', detail: '' };
  return { key: 'fail.generic', detail: '' };
}

/* 历史记录的一种状态配什么色调和图标：[标签的样式, 图标名]。
   历史页的列表和提问页的「最近问过」共用，免得同一条记录在两处长得不一样。 */
const HISTORY_TONES = {
  success: ['chip-ok', 'check'],
  completed: ['', 'check'],
  no_data: ['chip-info', 'empty'],
  review: ['chip-warn', 'warn'],
  partial: ['chip-warn', 'warn'],
  degraded: ['chip-warn', 'info'],
  failed: ['chip-danger', 'error'],
  conversation: ['', 'ask'],
};
export const historyTone = (status) => HISTORY_TONES[status] || HISTORY_TONES.completed;

/* 提示块。role 一律是 note：内容区整块重绘，role="alert" 的话每重画一次，
   页面上每一条旧的失败提示都会被读屏再念一遍。新出现的结果由 announce() 播报一次。 */
export function notice(tone, glyph, title, body) {
  return html`<div class="notice notice-${tone}" role="note">
    ${icon(glyph)}<div><p class="notice-title">${title}</p><div class="notice-body">${body}</div></div>
  </div>`;
}
