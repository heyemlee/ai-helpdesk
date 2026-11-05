/* 服务端接口。页面只通过这里拿数据：不直连数据库，不读本地文件，
   手里唯一的凭证是用户自己填的 API Key（只放在请求头里）。 */

import { effectiveUserId, state, uid } from './state.js';

export class ApiError extends Error {
  constructor({ status = 0, code = '', message = '', detail = null }) {
    super(message || code || `HTTP ${status}`);
    this.status = status;   // 0 = 请求没到服务端（网络、跨域、服务没起）
    this.code = code;
    this.serverMessage = message || '';   // 服务端给的原话；没有就是空，不拿错误码顶替
    this.detail = detail;
  }
}

const url = (path) => (state.base ? state.base.replace(/\/+$/, '') : '') + path;

function headers(json) {
  const out = {};
  if (json) out['Content-Type'] = 'application/json';
  if (state.apiKey) out['X-API-Key'] = state.apiKey;
  return out;
}

/* FastAPI 的 detail 有三种形状：字符串、{code, message}（数据浏览接口）、
   校验错误的数组（422）。统一成 ApiError。 */
function toError(status, payload) {
  const detail = payload && typeof payload === 'object' ? payload.detail : null;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    return new ApiError({ status, code: detail.code || '', message: detail.message || '', detail });
  }
  if (Array.isArray(detail)) {
    const message = detail.map((item) => item && item.msg).filter(Boolean).join('; ');
    return new ApiError({ status, code: 'invalid_request', message, detail });
  }
  return new ApiError({ status, message: typeof detail === 'string' ? detail : '', detail });
}

async function request(method, path, { params, body, signal } = {}) {
  let target = url(path);
  if (params) {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    });
    const text = query.toString();
    if (text) target += '?' + text;
  }

  let response;
  try {
    response = await fetch(target, {
      method,
      signal,
      headers: headers(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (error && error.name === 'AbortError') throw error;
    throw new ApiError({ status: 0, code: 'network' });
  }

  let payload = null;
  let readable = true;
  try {
    payload = await response.json();
  } catch (error) {
    // 读响应体的途中被取消，仍然是「取消」，不是服务出了问题
    if (error && error.name === 'AbortError') throw error;
    readable = false;
  }
  if (!response.ok) throw toError(response.status, payload);
  // 状态码是成功，但响应体不是 JSON（被代理换成了登录页、中途断了）：这不是一个可用的结果
  if (!readable) throw new ApiError({ status: response.status, code: 'bad_response' });
  return payload;
}

const actAs = () => (state.userId ? { user_id: state.userId } : {});

export const api = {
  health: () => request('GET', '/health'),
  ready: () => request('GET', '/readyz'),

  whoami: () => request('GET', '/whoami', { params: actAs() }),
  catalog: ({ refresh = false } = {}) =>
    request('GET', '/catalog', { params: { ...actAs(), refresh: refresh ? 'true' : '' } }),
  table: (database, table) =>
    request('GET', `/catalog/${encodeURIComponent(database)}/${encodeURIComponent(table)}`, {
      params: actAs(),
    }),
  rows: (database, table, body, signal) =>
    request(
      'POST',
      `/catalog/${encodeURIComponent(database)}/${encodeURIComponent(table)}/rows`,
      { body: { ...actAs(), ...body }, signal },
    ),
  // 表概况：只有聚合结果（行数、取值分布、按月的记录数），没有记录本身
  profile: (database, table, body, signal) =>
    request(
      'POST',
      `/catalog/${encodeURIComponent(database)}/${encodeURIComponent(table)}/profile`,
      { body: { ...actAs(), ...body }, signal },
    ),

  history: (limit = 50) => request('GET', '/history', { params: { limit, ...actAs() } }),
  feedback: (traceId, verdict, comment) =>
    request('POST', '/feedback', { body: { trace_id: traceId, verdict, comment: comment || '' } }),

  query: (question, signal) =>
    request('POST', '/query', {
      signal,
      body: {
        user_id: effectiveUserId(),
        session_id: state.sessionId,
        query: question,
        enable_long_term: state.longTerm,
      },
    }),

  schemaStatus: () => request('GET', '/admin/schema/status'),
  reloadSchema: () => request('POST', '/admin/schema/reload'),
  resetCircuits: () => request('POST', '/admin/circuit/reset'),
  feedbackStats: () => request('GET', '/feedback/stats'),

  // 模型连接（管理员）。Key 只往服务端送，这几个接口都不把它带回来
  modelStatus: () => request('GET', '/admin/model'),
  // 这个连接的接口提供哪些模型（给下拉框）。不调模型、不保存
  listModels: (body) => request('POST', '/admin/model/models', { body }),
  testModel: (body) => request('POST', '/admin/model/test', { body }),
  saveModel: (body) => request('PUT', '/admin/model', { body }),
  clearModel: () => request('DELETE', '/admin/model'),
  // Embedding（Schema 检索用的向量模型）。保存会先用新模型把检索索引建好，要等几秒到几十秒
  listEmbeddingModels: (body) => request('POST', '/admin/model/embedding/models', { body }),
  testEmbedding: (body) => request('POST', '/admin/model/embedding/test', { body }),
  saveEmbedding: (body) => request('PUT', '/admin/model/embedding', { body }),
  clearEmbedding: () => request('DELETE', '/admin/model/embedding'),

  stream: streamQuery,
  // 请服务端停下一次正在跑的运行；运行已经结束会回 409，调用方不必理会
  cancelRun: (runId) =>
    request('POST', `/runs/${encodeURIComponent(runId)}/cancel`, { params: actAs() }),
};

/* A2A message/stream：POST + SSE。

   原生 EventSource 只能发 GET、带不了请求体与自定义头，所以用 fetch 读流自己拆帧。
   网络分块不等于事件边界：一个事件、甚至一个 UTF-8 字符都可能跨块，
   因此解码器开 stream 模式，缓冲区按空行切。

   帧的顺序由契约保证：task → 若干 status-update(working) → artifact-update →
   status-update(final)。onStage 收到的是服务端真实发出的阶段事件，一条不多一条不少。
   返回 { payload, stages }，payload 与 /query 的响应体同构（少一个 reason）。 */
async function streamQuery(question, { signal, onStage, onTask } = {}) {
  let response;
  try {
    response = await fetch(url('/a2a'), {
      method: 'POST',
      signal,
      headers: { ...headers(true), Accept: 'text/event-stream' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: uid(),
        method: 'message/stream',
        params: {
          message: {
            kind: 'message',
            role: 'user',
            messageId: uid(),
            contextId: state.sessionId,
            parts: [{ kind: 'text', text: question }],
            metadata: { user_id: effectiveUserId() },
          },
        },
      }),
    });
  } catch (error) {
    if (error && error.name === 'AbortError') throw error;
    throw new ApiError({ status: 0, code: 'network' });
  }

  const type = response.headers.get('content-type') || '';
  if (!type.includes('text/event-stream')) {
    // 鉴权、限流、流式未启用：都发生在流打开之前，回来的是普通 JSON-RPC 错误
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    const rpc = payload && payload.error;
    throw new ApiError({
      status: response.status,
      code: rpc && rpc.code === -32004 ? 'stream_unsupported' : 'rpc_error',
      message: (rpc && rpc.message) || '',
      detail: rpc || payload,
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  const stages = [];
  let buffer = '';
  let startedAt = null;
  let payload = null;
  let terminal = null;

  const handle = (frame) => {
    if (!frame || typeof frame !== 'object') return;
    const stamp = Date.parse(frame.status && frame.status.timestamp);
    if (frame.kind === 'task') {
      if (!Number.isNaN(stamp)) startedAt = stamp;
      // taskId 就是服务端这次运行的编号：停止按钮拿它去请服务端停下（POST /runs/{id}/cancel）
      if (onTask && frame.id) onTask(frame.id);
    } else if (frame.kind === 'status-update' && !frame.final) {
      const meta = frame.metadata || {};
      const stage = {
        stage: meta.stage || '',
        step: meta.step ?? null,
        database: meta.database || '',
        shortPath: typeof meta.short_path === 'boolean' ? meta.short_path : null,
        withMemory: typeof meta.with_memory === 'boolean' ? meta.with_memory : null,
        offsetMs: startedAt != null && !Number.isNaN(stamp) ? stamp - startedAt : null,
      };
      stages.push(stage);
      if (onStage) onStage(stage);
    } else if (frame.kind === 'artifact-update') {
      const parts = (frame.artifact && frame.artifact.parts) || [];
      const data = parts.find((part) => part && part.kind === 'data');
      const text = parts.find((part) => part && part.kind === 'text');
      payload = { ...(data ? data.data : {}), answer: text ? text.text : '' };
    } else if (frame.kind === 'status-update' && frame.final) {
      terminal = frame;
    }
  };

  const drain = (flush) => {
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = flush ? '' : events.pop();
    events.forEach((block) => {
      const data = block
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n');
      if (!data) return;
      let envelope = null;
      try { envelope = JSON.parse(data); } catch (_) { return; }
      if (envelope && envelope.error) {
        throw new ApiError({ status: 200, code: 'rpc_error', message: envelope.error.message || '' });
      }
      handle(envelope && envelope.result);
    });
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: true });
      drain(done);
      if (done) break;
    }
  } catch (error) {
    reader.cancel().catch(() => {});
    if (error instanceof ApiError || (error && error.name === 'AbortError')) throw error;
    throw new ApiError({ status: 0, code: 'stream_interrupted' });
  }

  const state_ = terminal && terminal.status && terminal.status.state;
  if (state_ === 'failed') {
    throw new ApiError({
      status: 500,
      code: 'query_failed',
      message: (terminal.metadata && terminal.metadata.error) || '',
      detail: terminal.metadata || null,
    });
  }
  if (!payload || state_ !== 'completed') {
    // 收到载荷还要等到完成终态才算数；流中途断了就是没拿到结果
    throw new ApiError({ status: 0, code: 'stream_interrupted' });
  }
  if (!payload.trace_id && terminal.metadata) payload.trace_id = terminal.metadata.trace_id || '';
  return { payload, stages };
}
