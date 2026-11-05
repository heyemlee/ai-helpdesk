/* 数据目录的页面侧缓存。

   目录从 /catalog 取：有哪些业务域、表、字段，各自的业务名、单位、说明。
   三个视图都用它——数据浏览直接展示它，问答用它给结果列配上业务名与说明，
   首页用它决定哪些示例问题在当前权限下问得出结果。页面里没有任何一份
   写死的表清单：这里拿不到，页面就说「拿不到」。 */

import { api } from './api.js';
import { labelOf } from './format.js';
import { pick } from './i18n.js';
import { emit, state } from './state.js';

export const catalog = {
  status: 'idle',     // idle | loading | ready | error | unsupported | disabled
  error: null,
  data: null,
};

// 常见业务域的阅读顺序：先订单采购，再项目，再生产，再库存；认不出类型的排在后面
const KIND_ORDER = ['erp', 'pm', 'mes', 'wms'];
const rank = (domain) => {
  const index = KIND_ORDER.indexOf(domain.kind);
  return index === -1 ? KIND_ORDER.length : index;
};

let pending = null;
let latest = 0;

/* 身份与服务能力。平时跟着目录一起回来；目录拿不到（浏览被关掉、被限流、出错）时
   单独从 /whoami 取——能不能浏览数据和能不能提问是两回事，后者不该被前者连累。 */
function applyIdentity(payload) {
  state.identity = (payload && payload.identity) || null;
  state.service = (payload && payload.service) || null;
}

export function loadCatalog({ refresh = false } = {}) {
  if (pending && !refresh) return pending;
  // 已经有一份目录时是静默刷新：等新的回来再通知，免得页面先闪回「加载中」
  if (!catalog.data) {
    catalog.status = 'loading';
    emit('catalog');
  }

  // 后发的请求说了算。先发的那个晚回来时不能把新的目录和身份盖掉
  //（在设置里换了身份、紧接着又点了刷新，就是这种情况）。
  const ticket = ++latest;
  const current = api.catalog({ refresh })
    .then((data) => {
      if (ticket !== latest) return;
      data.domains = [...(data.domains || [])].sort((a, b) => rank(a) - rank(b));
      catalog.data = data;
      catalog.status = 'ready';
      catalog.error = null;
      applyIdentity(data);
    })
    .catch(async (error) => {
      if (ticket !== latest) return;
      catalog.data = null;
      catalog.error = error;
      // 404 有两种：这台服务关掉了数据浏览，或者它是没有这组接口的旧版本进程
      if (error.status === 404) catalog.status = error.code === 'disabled' ? 'disabled' : 'unsupported';
      else catalog.status = 'error';
      try {
        const who = await api.whoami();
        if (ticket === latest) applyIdentity(who);
      } catch (_) { /* 连身份也问不到：提问时由那次请求自己报出原因 */ }
    })
    .finally(() => {
      if (ticket !== latest) return;
      pending = null;
      emit('catalog');
    });
  pending = current;
  return current;
}

/* 发问之前确认「我是谁」。/query 的请求体必须带 user_id，而绑定了用户的 Key
   只接受它自己的那个——拿占位值去问，得到的是一个莫名其妙的 403。 */
export async function ensureIdentity() {
  if (catalog.status === 'idle') loadCatalog();
  while (pending) {
    const waiting = pending;
    try { await waiting; } catch (_) { /* 结果看 state，不看这个 promise */ }
    if (pending === waiting) break;
  }
  if (state.identity) return true;
  try {
    applyIdentity(await api.whoami());
    emit('catalog');
  } catch (_) { /* 照常发问：服务不在、Key 不对，都由那次请求如实报出来 */ }
  return Boolean(state.identity);
}

export const domains = () => (catalog.data ? catalog.data.domains : []);

export const findDomain = (database) => domains().find((domain) => domain.database === database) || null;

export function findTable(database, table) {
  const domain = findDomain(database);
  return domain ? domain.tables.find((item) => item.name === table) || null : null;
}

/* 业务域的名字：语义层写的 / 服务端按系统类型给的，都没有就退回别名或标识符。 */
export function domainTitle(domain) {
  if (!domain) return '';
  return pick(domain.title) || labelOf({ name: domain.database, aliases: domain.aliases });
}

export const domainTitleOf = (database) => {
  const domain = findDomain(database);
  return domain ? domainTitle(domain) : database;
};

/* 表很多的部署里 /catalog 不带字段，用到某张表时再取。 */
export async function ensureColumns(database, table) {
  const entry = findTable(database, table);
  if (!entry) return null;
  if (Array.isArray(entry.columns)) return entry;
  const detail = await api.table(database, table);
  Object.assign(entry, detail);
  return entry;
}

/* 给结果列找数据字典里的条目，用来显示业务名与说明。

   结果列只有一个名字，不知道出自哪张表。同一个库里同名字段可能不止一个：
   优先取 SQL 里提到的那张表上的；仍然不唯一、而且各处含义不同的，就不配——
   宁可只显示字段名，也不把另一张表的说明安到它头上。
   这是给人看的注解，不参与任何权限或数值判断。 */
export function lookupColumn(database, name, sql = '') {
  const domain = findDomain(database);
  if (!domain) return null;
  const matches = [];
  domain.tables.forEach((table) => {
    (table.columns || []).forEach((column) => {
      if (column.name === name) matches.push({ table, column });
    });
  });
  if (!matches.length) return null;
  if (matches.length === 1) return matches[0];

  const mentioned = matches.filter(({ table }) =>
    new RegExp(`\\b${table.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(sql));
  if (mentioned.length === 1) return mentioned[0];

  const pool = mentioned.length ? mentioned : matches;
  const same = pool.every(({ column }) =>
    column.description === pool[0].column.description && column.unit === pool[0].column.unit);
  return same ? pool[0] : null;
}
