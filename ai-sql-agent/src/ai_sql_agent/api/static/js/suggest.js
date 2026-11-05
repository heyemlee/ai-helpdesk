/* 提问页的建议问题：从数据目录里现成的东西拼出来，一个字也不写死。

   以前这里是几条写死的示例，按「业务域是哪一类系统」决定显不显示。换了一套库之后
   （系统类型没变，表和字段全换了），那几条照样显示，问的却是这套库里没有的数据。
   所以现在每一条都是「表的名字 + 字段的名字」按字段在数据字典里的角色拼的：

   - 声明了固定取值的分类字段 → 按它统计数量 / 筛出其中一个取值；
   - 数值字段（指标）        → 排出最高的几条 / 按分类字段看平均值；
   - 日期字段                → 按月统计。

   指标只拼「最高的几条」和「平均」，不拼「合计」：数据字典只说一个字段是指标，没说它能不能
   相加——准时率、单价这样的字段加起来没有意义，而页面分不出它们和金额的区别。

   目录里没有的表和字段不会出现在建议里；数据字典没给角色、取值的表拼不出建议，
   就不给——宁可少，也不拿猜的顶上。拼出来的只是一句话头，放进输入框由人改了再发。 */

import { labelOf } from './format.js';
import { t } from './i18n.js';
import { state } from './state.js';

export const PAGE_SIZE = 4;
// 问法一共这五种，文案在字典里是 suggest.<问法>
export const SUGGESTION_KINDS = ['countBy', 'topBy', 'perMonth', 'averageBy', 'whereIs'];
// 一个取值最长多少个字符：和服务端的 MAX_FACET_VALUE_CHARS 是同一个数，两边读出来的清单才一样
const MAX_OPTION_LENGTH = 120;
const AVAILABLE = 'available';

/* 数据字典里声明的固定取值 → 取值列表。

   约定的写法是「已签单 / 生产中 / 已交付」，只认斜杠——和服务端统计取值分布时认的是
   同一个写法（catalog/profile.py 的 declared_values，测试里拿两边对同一批写法各读一遍）。
   写成范围或一句话的（「0-1」、「正数，单位美元」）不是清单，返回空。
   比服务端多一条：只认文字字段——数字字段上的「0 / 1」拼成「筛出取值为 0 的」不像人会问的话。 */
export function declaredValues(column) {
  const text = String((column && column.value_range) || '');
  if (!text.includes('/') || column.kind !== 'text') return [];
  const parts = text.split('/').map((part) => part.trim());
  if (parts.some((part) => !part || part.length > MAX_OPTION_LENGTH)) return [];
  return [...new Set(parts)];
}

/* 英文句子中间的名字：「Order status」→「order status」。全大写的缩写（PO、ID）不动。 */
function inSentence(label) {
  if (state.lang !== 'en') return label;
  const first = label.split(/\s/, 1)[0];
  if (first.length > 1 && first === first.toUpperCase()) return label;
  return label.charAt(0).toLowerCase() + label.slice(1);
}

const name = (entity) => inSentence(labelOf(entity));
const usable = (column) => column.status === AVAILABLE;
const isCategory = (column) => ['filter', 'dimension'].includes(column.role) && declaredValues(column).length > 0;
const isMetric = (column) => ['metric', 'metric_filter'].includes(column.role) && column.kind === 'number';
const isTime = (column) => column.role === 'time' || column.kind === 'date';

/* 一张表拼得出的建议，按 SUGGESTION_KINDS 的顺序。每一条：{ id, kind, database, table, text }。 */
export function tableSuggestions(domain, table) {
  const columns = (table.columns || []).filter(usable);
  const categories = columns.filter(isCategory);
  const metrics = columns.filter(isMetric);
  const times = columns.filter(isTime);
  // 带单位的指标（金额）排在前面：比一个光秃秃的数量更像人会问的东西
  const metric = metrics.find((column) => column.unit) || metrics[0];
  const category = categories[0];
  // 「筛出一个取值」尽量换一个字段：和「按它统计」用同一个字段的话，两条建议看着像一条
  const picked = categories[1] || categories[0];

  const make = (kind, column, vars) => ({
    id: [domain.database, table.name, kind, column.name].join('.'),
    kind,
    database: domain.database,
    table: table.name,
    text: t('suggest.' + kind, { table: name(table), ...vars }),
  });

  const out = [];
  if (category) out.push(make('countBy', category, { column: name(category) }));
  if (metric) out.push(make('topBy', metric, { metric: name(metric) }));
  if (times[0]) out.push(make('perMonth', times[0], { column: name(times[0]) }));
  if (metric && category) out.push(make('averageBy', category, { metric: name(metric), column: name(category) }));
  if (picked) out.push(make('whereIs', picked, { column: name(picked), value: declaredValues(picked)[0] }));
  return out;
}

/* 一个库里各张表的建议交错着排：同一张表的五种问法不挨在一起。 */
function domainSuggestions(domain) {
  const lists = (domain.tables || [])
    .filter((table) => table.status === AVAILABLE)
    .map((table) => tableSuggestions(domain, table));
  const out = [];
  for (let round = 0; lists.some((list) => round < list.length); round += 1) {
    lists.forEach((list) => { if (round < list.length) out.push(list[round]); });
  }
  return out;
}

/* 全部建议，排成一页一页看的顺序。

   一轮里每个库出一条，而且尽量是这一轮还没出现过的问法——第一页于是每个库一条、
   问法各不相同。同一份目录、同一种语言，排出来的顺序永远一样：不随机，
   刷新之后看到的还是刚才那几条。 */
export function buildSuggestions(domains) {
  const queues = (domains || [])
    .filter((domain) => domain.status === AVAILABLE)
    .map(domainSuggestions)
    .filter((list) => list.length);
  const out = [];
  while (queues.some((queue) => queue.length)) {
    const used = new Set();
    queues.forEach((queue) => {
      if (!queue.length) return;
      const fresh = queue.findIndex((item) => !used.has(item.kind));
      const [item] = queue.splice(fresh === -1 ? 0 : fresh, 1);
      used.add(item.kind);
      out.push(item);
    });
  }
  return out;
}

export const pageCount = (list) => Math.max(1, Math.ceil(list.length / PAGE_SIZE));

/* 第 page 页（从 0 起，超过了就绕回开头）。 */
export function pageOf(list, page) {
  const start = (((page % pageCount(list)) + pageCount(list)) % pageCount(list)) * PAGE_SIZE;
  return list.slice(start, start + PAGE_SIZE);
}
