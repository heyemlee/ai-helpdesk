/* 设置里的「Embedding」：Schema 检索把表、字段的说明变成向量时用哪个模型。

   和上面的「模型连接」分开存、分开恢复：那块管回答问题的对话模型，这块只管检索。
   本地哈希向量不调服务、没有语义；换成真正的 Embedding 之后，「多少钱」这类说法才召得回金额字段。
   页面和上面一样：先写清楚现在用的是哪个，下拉框换模型，地址、Key、维度收在「连接设置」里。

   保存不只是改个配置：服务要先用新模型把整个检索索引重建一遍（几秒到几十秒），建成了才换上，
   中途失败什么都不动。所以保存前一定在页面里确认一次。Key 和上面一样只往服务端送，
   留空表示接着用这台主机上现有的那把。 */

import { api } from '../api.js';
import { $, html, icon } from '../dom.js';
import { t, tn } from '../i18n.js';
import { canKeepEmbeddingKey, embeddingPrefill, embeddingRequestBody, modelChoices, originOf } from '../modelForm.js';
import { notice } from '../steps.js';
import { currentModelStatus, keyText, metaLine, replaceModelStatus, savedText } from './settingsModel.js';

/* 下拉框里「手动输入」那一项的值。模型名不能以空格开头，撞不上真的名字。 */
const CUSTOM = ' custom';

let redraw = () => {};
let form = null;           // { baseUrl, model, dimensions }：表单里现在的值
let typedKey = '';         // 输入框里还没提交的 Key。只在内存里，保存成功或关掉对话框就清掉
let busy = '';             // '' | 'test' | 'save' | 'restore'
let report = null;         // 最近一次连接测试的结果
let outcome = null;        // 保存 / 恢复之后的那句话：{ ok, text }
let asking = '';           // '' | 'save' | 'restore'：页面里的确认条
let typing = false;        // 下拉框换成了手动输入
let connectionOpen = false;
let list = { target: null, phase: 'idle', result: null, error: '' };   // 下拉框的候选从哪来
let listSeq = 0;

/* 对话框刚打开：上次没保存就关掉的输入不留。 */
export function resetEmbeddingSection() {
  form = null;
  typedKey = '';
  busy = '';
  report = null;
  outcome = null;
  asking = '';
  typing = false;
  list = { target: null, phase: 'idle', result: null, error: '' };
  listSeq += 1;
}

/* 重画之前把输入框里的值记下来。 */
export function captureEmbeddingForm() {
  if (!form || !$('embBase')) return;
  const model = $('embModel').value;
  form = {
    baseUrl: $('embBase').value,
    model: model === CUSTOM ? form.model : model,
    dimensions: $('embDims').value,
  };
  typedKey = $('embKey').value;
}

function failureText(error) {
  if (error.serverMessage) return error.serverMessage;
  if (error.status === 0) return t('error.network');
  if (error.status === 401) return t('error.401');
  if (error.status === 403) return t('error.403');
  return t(error.status >= 500 ? 'error.5xx' : 'error.400');
}

function adopt(next) {
  replaceModelStatus(next);
  form = embeddingPrefill(next);
  typedKey = '';
  typing = false;
  asking = '';
}

/* 表单和现在用的那份比，有没有可存的改动。现在用的是本地向量的话，换成远端本身就是改动。 */
function changed() {
  const status = currentModelStatus();
  if (!status || !form) return false;
  const embedding = status.embedding;
  if (embedding.backend !== 'remote') return true;
  const trim = (value) => String(value || '').trim().replace(/\/+$/, '');
  return trim(form.baseUrl) !== trim(embedding.base_url) || form.model.trim() !== embedding.model
    || String(form.dimensions).trim() !== String(embedding.dimensions) || typedKey.trim() !== '';
}

const hasKey = (status) => typedKey.trim() !== '' || canKeepEmbeddingKey(status, form);

/* 读下拉框的候选：用表单里现在的地址和 Key。没有能用的 Key 就先不问。 */
async function loadList() {
  const status = currentModelStatus();
  if (!status || !status.editable) return;
  captureEmbeddingForm();
  if (!form) form = embeddingPrefill(status);
  const target = { baseUrl: form.baseUrl.trim(), key: typedKey.trim() };
  const seq = ++listSeq;
  if (!hasKey(status)) {
    list = { target, phase: 'needKey', result: null, error: '' };
    redraw();
    return;
  }
  list = { target, phase: 'loading', result: null, error: '' };
  redraw();
  try {
    const result = await api.listEmbeddingModels({ base_url: target.baseUrl, api_key: target.key });
    if (seq !== listSeq) return;
    list = { target, phase: 'ready', result, error: '' };
  } catch (error) {
    if (seq !== listSeq) return;
    list = { target, phase: 'error', result: null, error: failureText(error) };
  }
  captureEmbeddingForm();
  redraw();
}

/* 模型连接的状态刚取到：按现在的连接读一次候选。 */
export function embeddingStatusReady() {
  loadList();
}

const listStale = () => Boolean(list.target) && form
  && (list.target.baseUrl !== form.baseUrl.trim() || list.target.key !== typedKey.trim());

function listHint() {
  if (list.phase !== 'idle' && list.phase !== 'loading' && listStale()) return t('settings.model.list.stale');
  if (list.phase === 'needKey') return t('settings.model.list.needKey');
  if (list.phase === 'loading') return t('settings.model.list.loading');
  if (list.phase === 'error') return t('settings.model.list.failed', { reason: list.error });
  if (list.phase === 'ready' && !list.result.ok) {
    return t('settings.model.list.failed', { reason: t('settings.model.outcome.' + list.result.outcome) });
  }
  if (list.phase === 'ready') return tn('settings.model.list.count', list.result.models.length);
  return '';
}

async function run(kind, task) {
  captureEmbeddingForm();
  busy = kind;
  outcome = null;
  report = null;
  asking = '';
  redraw();
  try {
    await task();
  } catch (error) {
    // 保存前的测试没通过：服务端把测试结果一起带回来了，照样列出来
    if (error.detail && error.detail.test) report = error.detail.test;
    outcome = { ok: false, text: failureText(error) };
  }
  busy = '';
  redraw();
}

const test = () => run('test', async () => {
  report = await api.testEmbedding(embeddingRequestBody(form, typedKey));
});

/* 保存要重建整个检索索引：一律先在页面里确认。 */
function save({ confirmed = false } = {}) {
  captureEmbeddingForm();
  if (!currentModelStatus()) return;
  if (!confirmed) {
    asking = 'save';
    outcome = null;
    report = null;
    redraw();
    return;
  }
  const body = embeddingRequestBody(form, typedKey);
  run('save', async () => {
    const result = await api.saveEmbedding(body);
    adopt(result.status);
    report = result.test;
    outcome = {
      ok: true,
      text: t('settings.embedding.saved', {
        texts: result.rebuild.texts,
        seconds: (result.rebuild.duration_ms / 1000).toFixed(1),
      }),
    };
  });
}

function restore({ confirmed = false } = {}) {
  if (!currentModelStatus()) return;
  if (!confirmed) {
    captureEmbeddingForm();
    asking = 'restore';
    outcome = null;
    report = null;
    redraw();
    return;
  }
  run('restore', async () => {
    adopt((await api.clearEmbedding()).status);
    outcome = { ok: true, text: t('settings.embedding.restored') };
  }).then(() => loadList());
}

function cancel() {
  captureEmbeddingForm();
  asking = '';
  redraw();
}

export const embeddingActions = {
  'embedding-test': test,
  'embedding-save': () => save(),
  'embedding-confirm-save': () => save({ confirmed: true }),
  'embedding-restore': () => restore(),
  'embedding-confirm-restore': () => restore({ confirmed: true }),
  'embedding-cancel': cancel,
  'embedding-list': () => loadList(),
  'embedding-pick-list': () => {
    captureEmbeddingForm();
    typing = false;
    redraw();
  },
};

/* 下拉框选了一项。选「其他」就换成输入框。 */
export function embeddingPicked(value) {
  if (!form) return;
  if (value === CUSTOM) typing = true;
  captureEmbeddingForm();
  report = null;
  outcome = null;
  asking = '';
  redraw();
}

const keyPlaceholder = (status) => t(
  canKeepEmbeddingKey(status, form) ? 'settings.embedding.apiKey.keep' : 'settings.model.apiKey.required',
);

/* 地址、Key、维度、手填的模型名一边打字一边变：只改几处小地方，不重画——重画会把光标弄丢，
   正在点的保存键也会被换掉、那一下就点空了。 */
export function embeddingTyped() {
  captureEmbeddingForm();
  const status = currentModelStatus();
  if (!status) return;
  if ($('embKey')) $('embKey').placeholder = keyPlaceholder(status);
  if ($('embSave')) $('embSave').disabled = Boolean(busy) || !changed() || !hasKey(status);
  if ($('embPickHint')) $('embPickHint').textContent = listHint();
}

/* 「连接设置」展开 / 收起：整块重画之后保持原样。 */
export function embeddingConnectionToggled(open) {
  connectionOpen = open;
}

const hostOf = (address) => {
  try { return new URL(address).host; } catch (_) { return address || ''; }
};

/* 现在用的是哪个：检索索引实际在用的（`active`）优先——配的是远端、启动时它调不通的话，
   检索会退回本地哈希向量。 */
function inUse(embedding) {
  const active = embedding.active && embedding.active.backend ? embedding.active : embedding;
  const remote = active.backend === 'remote';
  let chip = '';
  if (embedding.fallback) {
    chip = html`<span class="chip chip-danger">${t('settings.embedding.warn.fallback')}</span>`;
  } else if (remote) {
    chip = html`<span class="chip chip-ok">${icon('check', 'icon-sm')}${t('settings.model.state.ok')}</span>`;
  }
  return html`<div class="model-now">
    <span class="model-now-label">${t('settings.model.current')}</span>
    <div class="model-now-main"><strong class="model-now-name">${remote ? active.model
      : t('settings.embedding.backend.local')}</strong>${chip}</div>
    ${metaLine([
      t('settings.embedding.report.dimensions', { dimensions: active.dimensions }),
      remote && embedding.endpoint && html`<span class="mono">${hostOf(embedding.endpoint)}</span>`,
      // 接口地址、Key 的末四位、谁保存的，只有能改的请求拿得到
      remote && embedding.key_hint !== undefined && keyText(embedding.key_configured, embedding.key_hint),
      !remote && t('settings.embedding.offline'),
      embedding.saved_at !== undefined && savedText(embedding.source, embedding.saved_at),
    ])}
  </div>`;
}

function warnings(embedding) {
  const items = [];
  if (embedding.fallback) {
    items.push(notice('danger', 'error', t('settings.embedding.warn.fallback'),
      html`${t('settings.embedding.warn.fallback.body')}<p class="original">${embedding.fallback}</p>`));
  }
  if (embedding.store_problem) {
    items.push(notice('warn', 'warn', t('settings.embedding.warn.storeProblem'),
      t('settings.embedding.warn.storeProblem.body')));
  }
  return items;
}

function reportBlock() {
  if (!report) return '';
  const lines = report.results.map((item) => html`<li><span class="mono">${item.model}</span>${t('settings.model.report.line', {
    roles: t('settings.model.role.embedding'),
    outcome: t('settings.model.outcome.' + item.outcome),
  })}${item.dimensions ? ` · ${t('settings.embedding.report.dimensions', { dimensions: item.dimensions })}` : ''}${item.status_code && !item.ok
    ? ` · HTTP ${item.status_code}` : ''} · ${item.duration_ms} ms${item.message
    ? html`<p class="original">${t('settings.model.report.server', { message: item.message })}</p>` : ''}</li>`);
  if (report.ok) {
    return html`<p class="model-ok">${icon('check', 'icon-sm')}<span>${t('settings.model.report.ok')} · ${report.results
      .map((item) => `${t('settings.embedding.report.dimensions', { dimensions: item.dimensions })} · ${item.duration_ms} ms`)
      .join(' · ')}</span></p>`;
  }
  const notes = (report.warnings || []).map((code) => html`<li>${t('settings.model.note.' + code)}</li>`);
  return notice('danger', 'error', t('settings.model.report.failed'), html`<ul>${lines}${notes}</ul>`);
}

/* 页面里的确认条，代替 window.confirm。 */
function confirmBar(status) {
  const embedding = status.embedding;
  const defaults = embedding.defaults || {};
  let text;
  if (asking === 'save') {
    const body = embeddingRequestBody(form, typedKey);
    text = t('settings.embedding.confirm.save', {
      address: originOf(body.base_url || defaults.base_url) || body.base_url,
      model: body.model || defaults.model,
    });
  } else {
    const server = embedding.server || {};
    text = t('settings.embedding.confirm.restore', {
      target: server.backend === 'remote'
        ? t('settings.embedding.backend.remote', { model: server.model, dimensions: server.dimensions })
        : t('settings.embedding.backend.local'),
    });
  }
  const save = asking === 'save';
  return html`<div class="notice notice-warn model-confirm" role="alertdialog" aria-label="${text}">
    ${icon('warn')}<div><p>${text}</p>
      <div class="row"><button type="button" class="btn btn-sm btn-primary" data-action="${save
        ? 'embedding-confirm-save' : 'embedding-confirm-restore'}">${t(save ? 'settings.embedding.save'
        : 'settings.model.confirm.restoreYes')}</button>
        <button type="button" class="btn btn-sm btn-ghost" data-action="embedding-cancel">${t('settings.model.cancel')}</button></div>
    </div>
  </div>`;
}

function picker(off) {
  if (typing) {
    return html`<input class="input" id="embModel" value="${form.model}" placeholder="${t('settings.model.typeName')}"
      autocomplete="off" spellcheck="false" ${off}>`;
  }
  const listed = list.phase === 'ready' && list.result.ok ? list.result.models : [];
  return html`<select class="select" id="embModel" ${off}>${modelChoices(listed, form.model).map((name) => html`<option
    value="${name}" ${name === form.model ? 'selected' : ''}>${name}</option>`)}<option
    value="${CUSTOM}">${t('settings.model.custom')}</option></select>`;
}

function formBlock(status) {
  const embedding = status.embedding;
  const defaults = embedding.defaults || {};
  const off = busy ? 'disabled' : '';
  return html`<form class="stack" data-form="embedding">
    <div class="field">
      <label for="embModel">${t('settings.embedding.model')}</label>
      <div class="model-pick-row">
        ${picker(off)}
        <button type="button" class="icon-btn" data-action="embedding-list" title="${t('settings.model.list.refresh')}"
          aria-label="${t('settings.model.list.refresh')}" ${busy || list.phase === 'loading' ? 'disabled' : ''}>${icon('refresh')}</button>
        <button type="submit" class="btn btn-sm btn-primary" id="embSave"
          ${busy || !changed() || !hasKey(status) ? 'disabled' : ''}>${t('settings.model.save')}</button>
      </div>
      <span class="hint" id="embPickHint">${listHint()}</span>
      ${typing ? html`<div><button type="button" class="link-btn" data-action="embedding-pick-list">${t('settings.model.fromList')}</button></div>` : ''}
    </div>
    ${asking ? confirmBar(status) : ''}
    <div class="stack-sm" role="status">
      ${busy ? html`<div class="loading"><span class="spinner"></span>${t('settings.embedding.busy.' + busy)}</div>` : ''}
      ${outcome && outcome.ok ? html`<p class="model-ok">${icon('check', 'icon-sm')}<span>${outcome.text}</span></p>` : ''}
      ${outcome && !outcome.ok ? notice('danger', 'error', t('settings.model.failed'), outcome.text) : ''}
      ${outcome && outcome.ok && report && report.ok ? '' : reportBlock()}
    </div>
    <details class="disclosure is-quiet" data-detail="embedding-connection" ${connectionOpen ? 'open' : ''}>
      <summary>${t('settings.model.connection')}</summary>
      <div class="disclosure-body stack">
        <div class="field"><label for="embBase">${t('settings.model.baseUrl')}</label>
          <input class="input" id="embBase" value="${form.baseUrl}" placeholder="${defaults.base_url || ''}"
            autocomplete="off" spellcheck="false" ${off}>
          <span class="hint">${t('settings.embedding.baseUrl.hint', { official: defaults.base_url || '' })}</span></div>
        <div class="field"><label for="embKey">${t('settings.model.apiKey')}</label>
          <input class="input" id="embKey" type="password" value="${typedKey}" placeholder="${keyPlaceholder(status)}"
            autocomplete="off" spellcheck="false" ${off}>
          <span class="hint">${t('settings.embedding.apiKey.hint')}</span></div>
        <div class="field"><label for="embDims">${t('settings.embedding.dimensions')}</label>
          <input class="input" id="embDims" inputmode="numeric" value="${form.dimensions}"
            placeholder="${String(defaults.dimensions || '')}" autocomplete="off" ${off}>
          <span class="hint">${t('settings.embedding.dimensions.hint')}</span></div>
        <div class="row">
          <button type="button" class="btn btn-sm" data-action="embedding-test" ${off}>${t('settings.model.test')}</button>
          ${embedding.source === 'page' || embedding.store_problem ? html`<button type="button" class="btn btn-sm btn-ghost"
            data-action="embedding-restore" ${off}>${t('settings.model.restore')}</button>` : ''}
        </div>
      </div>
    </details>
  </form>`;
}

/* 模型连接那一块还没取到状态（或者服务太老、没有 Embedding 这一节）时什么都不画：
   那一块已经在说明在取、取不到、还是没有权限。 */
export function embeddingSection() {
  const status = currentModelStatus();
  if (!status || !status.embedding) return '';
  if (!form) {
    form = embeddingPrefill(status);
    // 现在还是本地向量：要先填接口地址和 Key 才能选模型，「连接设置」默认展开
    connectionOpen = connectionOpen || status.embedding.backend !== 'remote';
  }
  const embedding = status.embedding;
  return html`<section class="stack model-settings">
    <h3>${t('settings.embedding')}</h3>
    <p class="desc">${t('settings.embedding.desc')}</p>
    ${inUse(embedding)}
    ${warnings(embedding)}
    ${status.editable ? formBlock(status) : ''}
  </section>`;
}

export function initEmbeddingSection(draw) {
  redraw = draw;
}
