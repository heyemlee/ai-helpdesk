/* 一步结果能不能画成一张图，画哪一种。

   只在形状没有歧义时画：正好一列名字加正好一列「量」，行数不多，而且这一步的结果是完整的。
   别的情况一律只给表格——猜着画出来的图，比没有图更容易让人看错。

   图只是同一批行的另一种看法，这里不做任何计算：不求和、不算占比、不补缺的月份、
   不把空值当成 0。哪一行画不出来（没有名字、数字是负的或者缺了），整张图就不画。 */

const MAX_BARS = 12;
const MAX_COLUMNS = 36;
const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
// 只认像年份的四位数。门店编号 1001、1002、1003 也是连着的四位数，但那不是时间
const YEAR = /^(19|20)\d{2}$/;
const AGGREGATE = /\b(count|sum|avg|min|max)\s*\(/i;
const METRIC_ROLES = ['metric', 'metric_filter'];

/* 数字那一列是不是一个「量」。是数字不等于是量：编号、年份、邮编都是数字。两种情况才算——

   - 数据字典里登记了这个字段，而且它的角色是指标；
   - 数据字典里没有它（查询里算出来的、改了名的列），而这条 SQL 里确实用了聚合函数：
     那是按分组数出来、加出来的数。

   登记了但不是指标的不算；没登记、SQL 里又没有聚合的，分不清是量还是编号，也不算
   （目录没拿到的时候每一列都是「没登记」，这时候只有聚合出来的结果会画图）。
   认的是数据字典和 SQL 这两样证据，不看列名像不像。 */
function isQuantity(column, sql) {
  if (column.plain) return false;
  if (column.entry) return METRIC_ROLES.includes(column.entry.role);
  return AGGREGATE.test(String(sql || ''));
}

/* 一串年份或年月是不是连着的（中间一个不缺）。缺了的话不能画成一排挨着的柱子：
   那样 3 月和 6 月贴在一起，看着像是连续的两个月。 */
function contiguous(labels) {
  const index = (label) => (MONTH.test(label)
    ? Number(label.slice(0, 4)) * 12 + Number(label.slice(5, 7))
    : Number(label));
  return labels.every((label, at) => at === 0 || index(label) - index(labels[at - 1]) === 1);
}

/* step：归一后的一步结果；columns：stepColumns(step) 的结果。
   返回 null（不画），或者 { kind: 'bars' | 'columns', label, value, items: [{ text, value }] }。 */
export function chartFor(step, columns) {
  if (!step || step.truncated || !Array.isArray(columns) || columns.length !== 2) return null;
  const value = columns.find((column) => column.numeric);
  const label = columns.find((column) => !column.numeric);
  if (!value || !label || !isQuantity(value, step.finalSql)) return null;
  const rows = step.rows || [];
  if (rows.length < 2 || rows.length > MAX_COLUMNS) return null;

  const seen = new Set();
  const items = [];
  for (const row of rows) {
    const name = row[label.key];
    const amount = row[value.key];
    if (name === null || name === undefined || name === '' || typeof name === 'object') return null;
    if (typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0) return null;
    const text = String(name);
    if (seen.has(text)) return null;       // 名字重复：一根条说的是哪一行，分不清
    seen.add(text);
    items.push({ text, value: amount });
  }
  if (!items.some((item) => item.value > 0)) return null;

  // 连着的年份或年月：按时间先后画成一排柱子
  const texts = items.map((item) => item.text);
  const periods = texts.every((text) => MONTH.test(text)) || texts.every((text) => YEAR.test(text));
  if (periods && items.length >= 3) {
    const sorted = [...items].sort((a, b) => a.text.localeCompare(b.text));
    if (contiguous(sorted.map((item) => item.text))) return { kind: 'columns', label, value, items: sorted };
  }
  // 其余的画成横条，保持查询返回的顺序（SQL 里的 ORDER BY 就是提问的人要的顺序）
  if (items.length > MAX_BARS) return null;
  return { kind: 'bars', label, value, items };
}

/* 一步结果有哪几种看法可以切换，第一个是默认的：
   - figures 只有一行：每一列是一个大数字，比一张只有一行的表好读；
   - chart   横条图。每一行的名字和数字都写在条的旁边，单独看不缺东西；
   - table   永远有。

   柱子（连着的年月）不算一种单独的看法：柱子上写不下数字，只能和表格放在一起看。 */
export function stepViews(step, columns, maxFigures = 8) {
  if (step.rowCount === 1 && step.rows.length === 1 && columns.length <= maxFigures) return ['figures', 'table'];
  const chart = chartFor(step, columns);
  return chart && chart.kind === 'bars' ? ['chart', 'table'] : ['table'];
}
