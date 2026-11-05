/* 设置里的「模型连接」：这台服务回答问题用哪家协议、哪个地址、哪把 Key、哪个模型。

   最上面写清楚现在用的是哪个模型；下面一个下拉框换模型、一个保存键。协议、接口地址、Key、
   SQL 单独用的模型这些不常改的，收在「连接设置」里。下拉框的候选是问接口要来的
   （POST /admin/model/models），读不到就退回手动输入。

   它和高级设置里那块「连接」不是一回事——那块是这个标签页怎么连服务，只影响自己；
   这块存在服务端，一保存，所有人的问题都发往新的地方。所以只有管理员能改，
   没开鉴权的服务只认本机；换了主机要在页面里再确认一次请求发给谁。
   保存前先测一次连接，测不通就不存（可以选「仍然保存」）。确认都在页面里做，
   不用 window.confirm：有的内嵌浏览器不弹它，保存会被悄悄取消。

   Key 只往服务端送，不从服务端取：状态里只有「配没配」和末四位，输入框永远是空的，
   留空表示接着用现有的那把。 */

import { api } from '../api.js';
import { $, html, icon } from '../dom.js';
import { t, tn } from '../i18n.js';
import {
  canKeepKey, insecurePage, knownRejection, modelChoices, originOf, prefill, requestBody,
} from '../modelForm.js';
import { state } from '../state.js';
import { notice } from '../steps.js';

const PROVIDERS = ['anthropic', 'openai'];
/* 下拉框里「手动输入」那一项的值。模型名不能以空格开头，撞不上真的名字。 */
const CUSTOM = ' custom';

let redraw = () => {};
let onReady = () => {};
let phase = 'idle';        // idle | loading | ready | error
let status = null;         // GET /admin/model 的结果
let failure = null;        // 取状态失败：{ status, message }
let form = null;           // { provider, baseUrl, chatModel, coderModel }：表单里现在的值
let typedKey = '';         // 输入框里还没提交的 Key。只在内存里，保存成功或关掉对话框就清掉
let busy = '';             // '' | 'test' | 'save' | 'restore'
let report = null;         // 最近一次连接测试的结果
let outcome = null;        // 保存 / 恢复之后的那句话：{ ok, text }
let asking = '';           // '' | 'save' | 'restore'：页面里的确认条
let failedSave = false;    // 保存前的测试没通过：给一个「仍然保存」
let coderSame = true;      // SQL 模型跟着上面那个走
let typing = { chat: false, coder: false };   // 下拉框换成了手动输入
let connectionOpen = false;
let list = { target: null, phase: 'idle', result: null, error: '' };   // 下拉框的候选从哪来
let listSeq = 0;

const isAdmin = () => Boolean(state.identity && state.identity.is_admin);

function adopt(next) {
  status = next;
  form = prefill(next, next.provider);
  typedKey = '';
  coderSame = form.coderModel === form.chatModel;
  typing = { chat: false, coder: false };
  asking = '';
  failedSave = false;
}

const describe = (error) => ({ status: error.status || 0, message: error.serverMessage || '' });

/* 服务端给了原话就用原话（哪个字段不对、为什么不让改，它说得比状态码清楚）。 */
function failureText(error) {
  if (error.message) return error.message;
  if (error.status === 0) return t('error.network');
  if (error.status === 401) return t('error.401');
  if (error.status === 403) return t('error.403');
  return t(error.status >= 500 ? 'error.5xx' : 'error.400');
}

async function load() {
  phase = 'loading';
  failure = null;
  redraw();
  try {
    adopt(await api.modelStatus());
    phase = 'ready';
  } catch (error) {
    failure = describe(error);
    phase = 'error';
  }
  redraw();
  if (phase === 'ready') {
    if (status.editable) loadList();
    onReady();
  }
}

/* Embedding 那一块和这里共用一份状态（GET /admin/model 一起回来的）。还没取到时是 null。 */
export const currentModelStatus = () => (phase === 'ready' ? status : null);

/* Embedding 保存 / 恢复之后换上服务端回来的新状态。这里表单里还没保存的输入不动。 */
export function replaceModelStatus(next) {
  if (next) status = next;
}

/* 对话框刚打开：上次没保存就关掉的输入不留，状态重新取。 */
export function resetModelSection() {
  phase = 'idle';
  status = null;
  form = null;
  typedKey = '';
  busy = '';
  report = null;
  outcome = null;
  asking = '';
  failedSave = false;
  typing = { chat: false, coder: false };
  list = { target: null, phase: 'idle', result: null, error: '' };
  listSeq += 1;
}

/* 对话框打开着、又还没取过状态时去取一次。不是管理员就不发请求——发了也只会被拒。 */
export function ensureModelLoaded() {
  if (phase === 'idle' && isAdmin()) load();
}

/* 重画之前把输入框里的值记下来：拨一下别的开关不该把刚填的地址和 Key 清掉。 */
export function captureModelForm() {
  if (!form || !$('modelProvider')) return;
  const chat = $('modelChat').value;
  const chatModel = chat === CUSTOM ? form.chatModel : chat;
  const coder = $('modelCoder');
  let coderModel = form.coderModel;
  if (coder.tagName === 'SELECT') {
    coderSame = coder.value === '';
    if (!coderSame && coder.value !== CUSTOM) coderModel = coder.value;
  } else {
    coderModel = coder.value;
  }
  form = {
    provider: $('modelProvider').value,
    baseUrl: $('modelBase').value,
    chatModel,
    coderModel: coderSame ? chatModel : coderModel,
  };
  typedKey = $('modelKey').value;
}

/* 表单和现在用的那份比，有没有可存的改动。 */
function changed() {
  if (!status || !form || !status.providers) return false;
  const current = status.providers[status.provider] || {};
  const trim = (value) => String(value || '').trim().replace(/\/+$/, '');
  return form.provider !== status.provider
    || trim(form.baseUrl) !== trim(current.base_url)
    || form.chatModel.trim() !== status.chat_model
    || form.coderModel.trim() !== status.coder_model
    || typedKey.trim() !== '';
}

/* 能不能点保存：有改动，而且这个地址上有能用的 Key（填了，或者同一台主机上现有的那把）。 */
const savable = () => changed() && (typedKey.trim() !== '' || canKeepKey(status, form));

/* 读下拉框的候选：用表单里现在的协议、地址和 Key。没有能用的 Key 就先不问。 */
async function loadList() {
  captureModelForm();
  const target = { provider: form.provider, baseUrl: form.baseUrl.trim(), key: typedKey.trim() };
  const seq = ++listSeq;
  if (!target.key && !canKeepKey(status, form)) {
    list = { target, phase: 'needKey', result: null, error: '' };
    redraw();
    return;
  }
  list = { target, phase: 'loading', result: null, error: '' };
  redraw();
  try {
    const result = await api.listModels({ provider: target.provider, base_url: target.baseUrl, api_key: target.key });
    if (seq !== listSeq) return;
    list = { target, phase: 'ready', result, error: '' };
  } catch (error) {
    if (seq !== listSeq) return;
    list = { target, phase: 'error', result: null, error: failureText(describe(error)) };
  }
  // 等列表的这一会儿，别处的输入框可能又填了东西：先记下再重画
  captureModelForm();
  redraw();
}

/* 列表是不是按表单里现在的地址和 Key 读的。不是的话提示重新读。 */
const listStale = () => Boolean(list.target) && (
  list.target.provider !== form.provider || list.target.baseUrl !== form.baseUrl.trim()
  || list.target.key !== typedKey.trim());

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

/* 下拉框里有哪些：接口报的；读不到时是现在用的和这家协议的默认值。 */
function choices(...current) {
  const listed = list.phase === 'ready' && list.result.ok ? list.result.models : [];
  const entry = (status.providers || {})[form.provider] || {};
  return modelChoices(listed, current, listed.length ? [] : [entry.chat_model, entry.coder_model]);
}

async function run(kind, task) {
  captureModelForm();
  busy = kind;
  outcome = null;
  report = null;
  asking = '';
  failedSave = false;
  redraw();
  try {
    await task();
  } catch (error) {
    outcome = { ok: false, text: failureText(describe(error)) };
  }
  busy = '';
  redraw();
}

const test = () => run('test', async () => {
  report = await api.testModel(requestBody(form, typedKey));
});

/* 换协议、换主机才要确认：同一台主机上换个模型，问题本来就发给它。 */
function needsConfirm(body) {
  const official = (status.providers[body.provider] || {}).official_base_url;
  return body.provider !== status.provider || originOf(body.base_url || official) !== originOf(status.endpoint);
}

function save({ confirmed = false, force = false } = {}) {
  captureModelForm();
  if (!savable()) return;
  const body = requestBody(form, typedKey);
  if (!confirmed && needsConfirm(body)) {
    asking = 'save';
    outcome = null;
    report = null;
    redraw();
    return;
  }
  run('save', async () => {
    if (!force) {
      report = await api.testModel(body);
      if (!report.ok) {
        failedSave = true;
        return;
      }
    }
    adopt((await api.saveModel(body)).status);
    outcome = { ok: true, text: t('settings.model.saved', { model: status.chat_model }) };
  });
}

function restore({ confirmed = false } = {}) {
  if (!confirmed) {
    asking = 'restore';
    outcome = null;
    report = null;
    redraw();
    return;
  }
  run('restore', async () => {
    adopt((await api.clearModel()).status);
    outcome = { ok: true, text: t('settings.model.restored') };
  }).then(() => { if (phase === 'ready' && status.editable) loadList(); });
}

function cancel() {
  captureModelForm();
  asking = '';
  redraw();
}

/* 输入框改回下拉框。 */
function pickFromList(target) {
  captureModelForm();
  typing = { ...typing, [target.dataset.which]: false };
  redraw();
}

export const modelActions = {
  'model-test': test,
  'model-save': () => save(),
  'model-confirm-save': () => save({ confirmed: true }),
  'model-save-anyway': () => save({ confirmed: true, force: true }),
  'model-restore': () => restore(),
  'model-confirm-restore': () => restore({ confirmed: true }),
  'model-cancel': cancel,
  'model-reload': load,
  'model-list': () => loadList(),
  'model-pick-list': pickFromList,
};

/* 换协议：地址和模型名跟着换成那一家的，Key 要不要重填由地址决定。 */
export function modelProviderChanged(provider) {
  if (!form || !PROVIDERS.includes(provider)) return;
  form = prefill(status, provider);
  typedKey = '';
  coderSame = form.coderModel === form.chatModel;
  typing = { chat: false, coder: false };
  report = null;
  outcome = null;
  asking = '';
  failedSave = false;
  redraw();
  loadList();
}

/* 下拉框选了一项。选「其他」就换成输入框，其余的照常记下。 */
export function modelPicked(id, value) {
  if (!form) return;
  if (id === 'modelChat' && value === CUSTOM) typing = { ...typing, chat: true };
  if (id === 'modelCoder' && value === CUSTOM) {
    typing = { ...typing, coder: true };
    coderSame = false;
  }
  captureModelForm();
  report = null;
  outcome = null;
  asking = '';
  failedSave = false;
  redraw();
}

const keyPlaceholder = () => t(canKeepKey(status, form) ? 'settings.model.apiKey.keep' : 'settings.model.apiKey.required');

/* 地址、Key、手填的模型名一边打字一边变：只改几处小地方，不重画——重画会把光标弄丢，
   正在点的保存键也会被换掉、那一下就点空了。 */
export function modelTyped() {
  captureModelForm();
  if ($('modelKey')) $('modelKey').placeholder = keyPlaceholder();
  if ($('modelSave')) $('modelSave').disabled = Boolean(busy) || !savable();
  if ($('modelPickHint')) $('modelPickHint').textContent = listHint();
}

/* 「连接设置」展开 / 收起：整块重画之后保持原样。 */
export function modelConnectionToggled(open) {
  connectionOpen = open;
}

const hostOf = (address) => {
  try { return new URL(address).host; } catch (_) { return address || ''; }
};

/* 「·」前面是不换行空格：窄屏折行时点留在行尾，不会跑到下一行开头。 */
export const metaLine = (items) => html`<p class="model-now-meta">${items.filter(Boolean).map((item, index) => html`${index
  ? html`<span class="model-now-sep" aria-hidden="true">\u00a0· </span>` : ''}<span>${item}</span>`)}</p>`;

export function savedText(source, savedAt) {
  if (source !== 'page') return t('settings.model.source.server');
  const when = (savedAt || '').replace('T', ' ').slice(0, 16);
  return when ? t('settings.model.source.pageAt', { when }) : t('settings.model.source.page');
}

export function keyText(configured, hint) {
  if (!configured) return t('settings.model.state.noKey');
  return hint ? t('settings.model.key.ends', { hint }) : t('settings.model.key.set');
}

function stateChip() {
  if (!status.key_configured) return html`<span class="chip chip-warn">${t('settings.model.state.noKey')}</span>`;
  // 只有能改的请求才拿得到「这把 Key 被拒过没有」：看不到的时候不替它说「可用」
  if (!status.editable) return '';
  const rejection = knownRejection(status);
  if (rejection) {
    return html`<span class="chip chip-danger">${t('settings.model.key.rejected')}${rejection.status_code
      ? ` · HTTP ${rejection.status_code}` : ''}</span>`;
  }
  return html`<span class="chip chip-ok">${icon('check', 'icon-sm')}${t('settings.model.state.ok')}</span>`;
}

/* 现在用的是哪个模型：名字放大，协议、主机、Key、来源放在下面一行小字。 */
function inUse() {
  return html`<div class="model-now">
    <span class="model-now-label">${t('settings.model.current')}</span>
    <div class="model-now-main"><strong class="model-now-name">${status.chat_model}</strong>${stateChip()}</div>
    ${metaLine([
      t('settings.model.provider.' + status.provider),
      status.coder_model !== status.chat_model && t('settings.model.coderIs', { model: status.coder_model }),
      status.endpoint && html`<span class="mono">${hostOf(status.endpoint)}</span>`,
      status.key_configured && keyText(true, status.key_hint),
      status.editable && savedText(status.source, status.saved_at),
    ])}
  </div>
  ${status.key_configured ? '' : html`<p class="desc">${t('settings.model.key.missing')}</p>`}`;
}

function warnings() {
  const items = [];
  if (insecurePage(window.location)) items.push('insecurePage');
  if (status.store_problem) items.push('storeProblem');
  if (status.custom_endpoint) items.push('customEndpoint');
  return items.map((name) => notice(
    'warn', 'warn', t('settings.model.warn.' + name), t('settings.model.warn.' + name + '.body'),
  ));
}

function reportBlock() {
  if (!report) return '';
  if (report.ok) {
    const took = report.results.map((item) => `${item.roles.map((role) => t('settings.model.role.' + role))
      .join(t('common.listSep'))} ${item.duration_ms} ms`).join(' · ');
    return html`<p class="model-ok">${icon('check', 'icon-sm')}<span>${t('settings.model.report.ok')} · ${took}</span></p>`;
  }
  const lines = report.results.map((item) => html`<li><span class="mono">${item.model}</span>${t('settings.model.report.line', {
    roles: item.roles.map((role) => t('settings.model.role.' + role)).join(t('common.listSep')),
    outcome: t('settings.model.outcome.' + item.outcome),
  })}${item.status_code && !item.ok ? ` · HTTP ${item.status_code}` : ''} · ${item.duration_ms} ms${item.message
    ? html`<p class="original">${t('settings.model.report.server', { message: item.message })}</p>` : ''}</li>`);
  const notes = (report.warnings || []).map((code) => html`<li>${t('settings.model.note.' + code)}</li>`);
  return notice('danger', 'error', t('settings.model.report.failed'), html`<ul>${lines}${notes}</ul>`);
}

/* 页面里的确认条，代替 window.confirm。 */
function confirmBar() {
  let text;
  if (asking === 'save') {
    const body = requestBody(form, typedKey);
    const official = (status.providers[body.provider] || {}).official_base_url;
    text = t('settings.model.confirm.save', { address: originOf(body.base_url || official) || body.base_url });
  } else {
    const server = status.server || {};
    text = t('settings.model.confirm.restore', {
      target: `${t('settings.model.provider.' + server.provider)} · ${server.chat_model}`,
    });
  }
  const yes = asking === 'save' ? 'model-confirm-save' : 'model-confirm-restore';
  return html`<div class="notice notice-warn model-confirm" role="alertdialog" aria-label="${text}">
    ${icon('warn')}<div><p>${text}</p>
      <div class="row"><button type="button" class="btn btn-sm btn-primary" data-action="${yes}">${t(asking === 'save'
        ? 'settings.model.confirm.yes' : 'settings.model.confirm.restoreYes')}</button>
        <button type="button" class="btn btn-sm btn-ghost" data-action="model-cancel">${t('settings.model.cancel')}</button></div>
    </div>
  </div>`;
}

/* 选模型的控件：下拉框，或者选了「其他」之后的输入框。`same` 是 SQL 模型那一格的第一项。 */
function picker(id, value, typed, off, same) {
  if (typed) {
    return html`<input class="input" id="${id}" value="${value}" placeholder="${t('settings.model.typeName')}"
      autocomplete="off" spellcheck="false" ${off}>`;
  }
  return html`<select class="select" id="${id}" ${off}>${same ? html`<option value="" ${coderSame ? 'selected' : ''}>${same}</option>` : ''}${
    choices(value).map((name) => html`<option value="${name}" ${!(same && coderSame) && name === value ? 'selected' : ''}>${name}</option>`)}<option
      value="${CUSTOM}">${t('settings.model.custom')}</option></select>`;
}

function formBlock() {
  const entry = status.providers[form.provider] || {};
  const off = busy ? 'disabled' : '';
  return html`<form class="stack" data-form="model">
    <div class="field">
      <label for="modelChat">${t('settings.model.model')}</label>
      <div class="model-pick-row">
        ${picker('modelChat', form.chatModel, typing.chat, off)}
        <button type="button" class="icon-btn" data-action="model-list" title="${t('settings.model.list.refresh')}"
          aria-label="${t('settings.model.list.refresh')}" ${busy || list.phase === 'loading' ? 'disabled' : ''}>${icon('refresh')}</button>
        <button type="submit" class="btn btn-sm btn-primary" id="modelSave" ${busy || !savable() ? 'disabled' : ''}>${t('settings.model.save')}</button>
      </div>
      <span class="hint" id="modelPickHint">${listHint()}</span>
      ${typing.chat ? html`<div><button type="button" class="link-btn" data-action="model-pick-list" data-which="chat">${t('settings.model.fromList')}</button></div>` : ''}
    </div>
    ${asking ? confirmBar() : ''}
    <div class="stack-sm" role="status">
      ${busy ? html`<div class="loading"><span class="spinner"></span>${t('settings.model.busy.' + busy)}</div>` : ''}
      ${outcome && outcome.ok ? html`<p class="model-ok">${icon('check', 'icon-sm')}<span>${outcome.text}</span></p>` : ''}
      ${outcome && !outcome.ok ? notice('danger', 'error', t('settings.model.failed'), outcome.text) : ''}
      ${outcome && outcome.ok && report && report.ok ? '' : reportBlock()}
      ${failedSave ? html`<div class="row"><span class="desc">${t('settings.model.notSaved')}</span><button type="button"
        class="btn btn-sm" data-action="model-save-anyway">${t('settings.model.saveAnyway')}</button></div>` : ''}
    </div>
    <details class="disclosure is-quiet" data-detail="model-connection" ${connectionOpen ? 'open' : ''}>
      <summary>${t('settings.model.connection')}</summary>
      <div class="disclosure-body stack">
        ${warnings()}
        <div class="field"><label for="modelProvider">${t('settings.model.provider')}</label>
          <select class="select select-auto" id="modelProvider" ${off}>${PROVIDERS.map((name) => html`<option
            value="${name}" ${form.provider === name ? 'selected' : ''}>${t('settings.model.provider.' + name)}</option>`)}</select></div>
        <div class="field"><label for="modelBase">${t('settings.model.baseUrl')}</label>
          <input class="input" id="modelBase" value="${form.baseUrl}" placeholder="${entry.official_base_url || ''}"
            autocomplete="off" spellcheck="false" ${off}>
          <span class="hint">${t('settings.model.baseUrl.hint', { official: entry.official_base_url || '' })}</span></div>
        <div class="field"><label for="modelKey">${t('settings.model.apiKey')}</label>
          <input class="input" id="modelKey" type="password" value="${typedKey}" placeholder="${keyPlaceholder()}"
            autocomplete="off" spellcheck="false" ${off}>
          <span class="hint">${t('settings.model.apiKey.hint')}</span></div>
        <div class="field"><label for="modelCoder">${t('settings.model.coderModel')}</label>
          ${picker('modelCoder', form.coderModel, typing.coder, off, t('settings.model.coderSame'))}
          ${typing.coder ? html`<div><button type="button" class="link-btn" data-action="model-pick-list" data-which="coder">${t('settings.model.fromList')}</button></div>` : ''}</div>
        <div class="row">
          <button type="button" class="btn btn-sm" data-action="model-test" ${off}>${t('settings.model.test')}</button>
          ${status.source === 'page' || status.store_problem ? html`<button type="button" class="btn btn-sm btn-ghost"
            data-action="model-restore" ${off}>${t('settings.model.restore')}</button>` : ''}
        </div>
      </div>
    </details>
  </form>`;
}

function body() {
  if (!isAdmin()) return html`<p class="desc">${t('settings.model.denied')}</p>`;
  if (phase === 'idle' || phase === 'loading') {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('settings.model.loading')}</div>`;
  }
  if (phase === 'error') {
    // 老版本的服务没有这组接口：不是出错，是没有这个功能
    if (failure.status === 404) return html`<p class="desc">${t('settings.model.unsupported')}</p>`;
    return html`<div class="stack-sm" role="status"><p class="desc">${t('settings.model.loadFailed')} ${failureText(failure)}</p>
      <div><button type="button" class="btn btn-sm" data-action="model-reload">${t('action.tryAgain')}</button></div></div>`;
  }
  return html`${inUse()}
    ${status.editable
      ? formBlock()
      : notice('info', 'lock', t('settings.model.reason.' + status.reason), t('settings.model.reason.' + status.reason + '.body'))}`;
}

export function modelSection() {
  return html`<section class="stack model-settings">
    <h3>${t('settings.model')}</h3>
    <p class="desc">${t('settings.model.desc')}</p>
    ${body()}
  </section>`;
}

/* `ready`：模型连接的状态取到之后调一次（Embedding 那一块借这个时机去读它的候选）。 */
export function initModelSection(draw, ready = () => {}) {
  redraw = draw;
  onReady = ready;
}
