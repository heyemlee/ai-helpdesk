/* 「模型连接」表单里不碰 DOM 的那几段判断。

   这些判断服务端都会再做一遍，而且以服务端的为准——页面上先做一次，
   是为了在点保存之前就把话说清楚：这把 Key 还能不能接着用、这个页面适不适合填 Key。 */

const WEB = { 'http:': true, 'https:': true };

/* 地址 → 「协议 + 主机 + 端口」。Key 是交给一台主机的，路径怎么写不改变它交给了谁。 */
export function originOf(address) {
  let url;
  try { url = new URL(String(address || '').trim()); } catch (_) { return ''; }
  if (!WEB[url.protocol] || !url.hostname) return '';
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  return `${url.protocol}//${host}${url.port ? ':' + url.port : ''}`;
}

const known = (status, provider) => (status && status.providers && status.providers[provider]) || {};

/* 切到某一家协议时表单里该填什么：这家现在配的地址和模型名（没配过就是默认值）。 */
export function prefill(status, provider) {
  const entry = known(status, provider);
  return {
    provider,
    baseUrl: entry.base_url || '',
    chatModel: entry.chat_model || '',
    coderModel: entry.coder_model || '',
  };
}

/* 不填 Key 行不行：这家协议已经有一把 Key，而且接口地址没有换主机。
   换了主机就必须重填——现有的 Key 不会被发到一个新的地址去。 */
export function canKeepKey(status, form) {
  const entry = known(status, form.provider);
  if (!entry.key_configured) return false;
  const target = originOf(String(form.baseUrl || '').trim() || entry.official_base_url);
  return Boolean(target) && target === entry.key_address;
}

export function isLocalHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  return host === 'localhost' || host.endsWith('.localhost') || host === '::1' || /^127(\.\d{1,3}){3}$/.test(host);
}

/* 页面是不是经过明文 http 从别的机器打开的：是的话，在这里填的 Key 会不加密地经过网络。 */
export function insecurePage(location) {
  return Boolean(location) && location.protocol === 'http:' && !isLocalHost(location.hostname);
}

/* 提交给服务端的请求体。Key 留空 = 接着用现有的。 */
export function requestBody(form, apiKey) {
  const text = (value) => String(value || '').trim();
  return {
    provider: form.provider,
    base_url: text(form.baseUrl),
    api_key: text(apiKey),
    chat_model: text(form.chatModel),
    coder_model: text(form.coderModel),
  };
}

/* 下拉框的候选：接口报的模型名、现在用的、默认的，去重后按字母排，空的不要。 */
export function modelChoices(...groups) {
  const names = new Set();
  groups.flat().forEach((name) => {
    const text = String(name || '').trim();
    if (text) names.add(text);
  });
  return [...names].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
}

/* 这把 Key 是不是已经被模型服务明确拒绝过（凭证无效、停用、额度用尽）。
   服务记下之后不会再拿它去调模型，所以这里能直接说出来，不用等下一个问题失败。 */
export function knownRejection(status) {
  const faults = (status && status.access_faults) || [];
  return faults.find((fault) => fault && fault.permanent && fault.scope === 'credential') || null;
}

/* ---- Embedding ---------------------------------------------------------------- */

const embeddingOf = (status) => (status && status.embedding) || {};

/* Embedding 表单里该填什么：现在用的是远端就照它填，否则填默认值（OpenAI 官方地址）。 */
export function embeddingPrefill(status) {
  const embedding = embeddingOf(status);
  const defaults = embedding.defaults || {};
  const remote = embedding.backend === 'remote';
  return {
    baseUrl: (remote && embedding.base_url) || defaults.base_url || '',
    model: (remote && embedding.model) || defaults.model || '',
    dimensions: String((remote && embedding.dimensions) || defaults.dimensions || ''),
  };
}

/* Embedding 的 Key 能不能留空：填的地址落在已经有 Key 的主机上——现在 Embedding 用的那台，
   或者对话模型那台（两边都是 OpenAI 时不用把同一把 Key 填两遍）。换了主机就必须重填。 */
export function canKeepEmbeddingKey(status, form) {
  const embedding = embeddingOf(status);
  const addresses = embedding.reusable_key_addresses || [];
  const target = originOf(String(form.baseUrl || '').trim() || (embedding.defaults || {}).base_url);
  return Boolean(target) && addresses.includes(target);
}

/* 提交给服务端的 Embedding 请求体。Key 留空 = 接着用现有的；维度不是正整数 = 用默认值。 */
export function embeddingRequestBody(form, apiKey) {
  const text = (value) => String(value || '').trim();
  const dimensions = Number(text(form.dimensions));
  return {
    base_url: text(form.baseUrl),
    api_key: text(apiKey),
    model: text(form.model),
    dimensions: Number.isInteger(dimensions) && dimensions > 0 ? dimensions : 0,
  };
}
