/* 历史记录的页面侧缓存。问答页用它回填服务端记账的耗时与 token，历史页用它列记录。

   服务端的历史来自审计表，只有问题、SQL 与元数据，**没有结果集**。
   某条记录在这个标签页里有没有结果可看，取决于 state.resultCache，不取决于这里。 */

import { api } from './api.js';
import { emit } from './state.js';

export const history = {
  status: 'idle',     // idle | loading | ready | error
  items: [],
  error: null,
  loadedAt: null,
};

let pending = null;
let generation = 0;     // 每换一次身份加一：在途的那次请求回来时对不上号，结果就作废

export function loadHistory() {
  if (pending) return pending;
  if (history.status !== 'ready') history.status = 'loading';
  emit('history');
  const ticket = generation;
  const current = api.history(50)
    .then((data) => {
      if (ticket !== generation) return;
      history.items = Array.isArray(data.items) ? data.items : [];
      history.status = 'ready';
      history.error = null;
      history.loadedAt = new Date().toISOString();
    })
    .catch((error) => {
      if (ticket !== generation) return;
      history.status = 'error';
      history.error = error;
    })
    .finally(() => {
      if (pending === current) pending = null;
      if (ticket === generation) emit('history');
    });
  pending = current;
  return current;
}

/* 换了服务地址、Key 或「以谁的身份」：手里这份历史是上一个身份问过的问题，不能再显示。
   清空，并让还在路上的那次请求作废。要不要马上重新取由调用方定（看哪一页正开着）。 */
export function resetHistory() {
  generation += 1;
  pending = null;
  history.items = [];
  history.status = 'idle';
  history.error = null;
  history.loadedAt = null;
  emit('history');
}

export const findHistory = (traceId) => history.items.find((item) => item.trace_id === traceId) || null;
