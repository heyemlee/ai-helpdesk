/* 表概况（行数、取值分布、按月的记录数）的页面侧取数与缓存。

   概况从 /catalog/{库}/{表}/profile 取，是服务端按当前身份算出来的聚合结果，
   页面只管画。同一张表、同一组条件只取一次；目录一刷新（换了身份、点了刷新、
   断线重连）这里的东西就全部作废——它们是按上一份目录、上一个身份算的。

   目录页一打开要给每张表各取一次行数，所以取数排队走，同时最多 MAX_PARALLEL 个：
   一口气发几十个请求，先被限流的是用户自己接下来要点的那一下。 */

import { api } from './api.js';
import { catalog } from './catalog.js';
import { emit, state } from './state.js';

const MAX_PARALLEL = 4;
const MAX_ENTRIES = 200;

const entries = new Map();     // 键 → { status: 'loading' | 'ready' | 'error', data, error }
const queue = [];
let running = 0;
let snapshot = null;           // 这些概况是对着哪一份目录取的

/* 这台服务给不给概况：'on' | 'off'（管理员关了）| 'unsupported'（没有这组接口的旧版本进程）
   | 'unknown'（还没问到）。 */
export function profileSupport() {
  if (!state.service) return 'unknown';
  const info = state.service.profile;
  if (!info) return 'unsupported';
  return info.enabled ? 'on' : 'off';
}

const keyOf = (database, table, search, filters, breakdowns) => JSON.stringify([
  database, table, search || '',
  (filters || []).map((filter) => [filter.column, filter.op, String(filter.value ?? '')]),
  breakdowns ? 1 : 0,
]);

function sync() {
  if (snapshot === catalog.data) return;
  snapshot = catalog.data;
  entries.clear();
  queue.length = 0;
}

function pump() {
  while (running < MAX_PARALLEL && queue.length) {
    const job = queue.shift();
    if (job.signal && job.signal.aborted) { entries.delete(job.entry.key); continue; }
    if (entries.get(job.entry.key) !== job.entry) continue;      // 排队期间目录换过了
    running += 1;
    api.profile(job.database, job.table, job.body, job.signal)
      .then((data) => { Object.assign(job.entry, { status: 'ready', data, error: null }); })
      .catch((error) => {
        if (error && error.name === 'AbortError') { entries.delete(job.entry.key); return; }
        Object.assign(job.entry, { status: 'error', data: null, error });
      })
      .finally(() => {
        running -= 1;
        // 取回来的时候目录已经换了：这份结果不属于现在这一页，不通知
        if (entries.get(job.entry.key) === job.entry) emit('profile', job.entry);
        pump();
      });
  }
}

/* 要一份概况。立刻返回一个条目（可能还在 loading），取到之后发 'profile' 事件。
   服务不给概况时返回 null。breakdowns=false 只要行数与各字段的填充数。 */
export function requestProfile(database, table, {
  search = '', filters = [], breakdowns = true, signal = null,
} = {}) {
  if (profileSupport() !== 'on' || catalog.status !== 'ready') return null;
  sync();

  const key = keyOf(database, table, search, filters, breakdowns);
  const known = entries.get(key);
  if (known) return known;
  if (!breakdowns) {
    // 只要行数，而带分布的那一份已经有了：它里面就有
    const full = entries.get(keyOf(database, table, search, filters, true));
    if (full && full.status !== 'error') return full;
  }

  const entry = { key, database, table, status: 'loading', data: null, error: null };
  entries.set(key, entry);
  if (entries.size > MAX_ENTRIES) entries.delete(entries.keys().next().value);
  queue.push({ entry, database, table, signal, body: { search, filters, breakdowns } });
  pump();
  return entry;
}

/* 已经要过的那一份（可能还在 loading），没要过就是 null。只看，不发请求：
   画目录、画一个库的卡片时用这个——画一遍页面不该顺手发出几十个请求。 */
export function findProfile(database, table, { breakdowns = true } = {}) {
  sync();
  const full = entries.get(keyOf(database, table, '', [], true));
  if (breakdowns) return full || null;
  if (full && full.status !== 'error') return full;
  return entries.get(keyOf(database, table, '', [], false)) || full || null;
}

/* 出错的那一份丢掉重取。 */
export function retryProfile(entry, options) {
  if (entry) entries.delete(entry.key);
  return entry ? requestProfile(entry.database, entry.table, options) : null;
}

/* 一张表没有筛选时、带分布的那份概况：已经取到就给，没有就是 null。只看，不发请求——
   别的表的配色（这个取值是第几个颜色）用得上时顺手用，不为它专门去取。 */
export function peekProfile(database, table) {
  sync();
  const entry = entries.get(keyOf(database, table, '', [], true));
  return entry && entry.status === 'ready' ? entry.data : null;
}

/* 一张表（没有筛选时）的总行数：number，或者还不知道时是 null。只看已经取到的，不发请求。 */
export function knownRowCount(database, table) {
  sync();
  for (const breakdowns of [true, false]) {
    const entry = entries.get(keyOf(database, table, '', [], breakdowns));
    if (entry && entry.status === 'ready' && typeof entry.data.total_rows === 'number') {
      return entry.data.total_rows;
    }
  }
  return null;
}
