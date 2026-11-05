/* 设置。上半部分是每个人都可能用到的两项（语言、学习模式），管理员还能在这里直接看到、
   换掉这台服务用的模型（模型连接、Embedding）；连接配置、诊断、运维操作收在「高级设置」里——
   查数的员工不需要知道服务地址和 trace 是什么，也不该被这些东西挡在问题前面。 */

import { api } from '../api.js';
import { catalog } from '../catalog.js';
import { $, delegate, html, icon, setHtml } from '../dom.js';
import { t } from '../i18n.js';
import { emit, on, save, state } from '../state.js';
import {
  captureModelForm,
  ensureModelLoaded,
  initModelSection,
  modelActions,
  modelConnectionToggled,
  modelPicked,
  modelProviderChanged,
  modelSection,
  modelTyped,
  resetModelSection,
} from './settingsModel.js';
import {
  captureEmbeddingForm,
  embeddingActions,
  embeddingConnectionToggled,
  embeddingPicked,
  embeddingSection,
  embeddingStatusReady,
  embeddingTyped,
  initEmbeddingSection,
  resetEmbeddingSection,
} from './settingsEmbedding.js';

let dialog;
let advancedOpen = false;
let diagnostics = null;        // { status: 'running' | 'done', rows: [[label, value, tone]] }
let operation = null;          // { name, status: 'running' | 'done' | 'error', output }

/* 开关。读屏念的名字是这一项设置的名字（「学习模式」），开没开由 aria-checked 说；
   旁边那个「开 / 关」是给眼睛看的，不能拿它当名字——那样每个开关都叫「关」。 */
function switchButton(action, checked, name) {
  return html`<button type="button" class="switch" role="switch" aria-checked="${checked ? 'true' : 'false'}"
    aria-label="${name}" data-action="${action}"><span class="track" aria-hidden="true"></span><span
    aria-hidden="true">${t(checked ? 'common.on' : 'common.off')}</span></button>`;
}

function identityRows() {
  const identity = state.identity;
  const service = state.service;
  const rows = [
    [t('settings.conversation'), html`<span class="mono">${state.sessionId}</span>`],
    [t('settings.identity'), identity
      ? html`<span class="mono">${identity.user_id}</span>${identity.anonymous ? ` · ${t('settings.identity.anonymous')}` : ''}${identity.is_admin
        ? ` · ${t('settings.identity.admin')}` : ''}`
      : t('settings.unknown')],
    [t('settings.catalog'), t('settings.catalog.' + catalog.status)],
  ];
  if (service) rows.push([t('settings.version'), html`<span class="mono">${service.version}</span> · ${t('settings.auth.' + service.auth_mode)}`]);
  return rows;
}

function draw({ fresh = false, fromModel = false, fromEmbedding = false } = {}) {
  const isAdmin = Boolean(state.identity && state.identity.is_admin);
  // 重画时保住连接表单里还没保存的输入：拨一下别的开关不该把刚填的地址清掉。
  // 刚打开对话框时（fresh）不保：上次没保存就关掉的输入，再打开时不该看起来像已经生效了。
  const typed = (id, saved) => (!fresh && $(id) ? $(id).value : saved);
  const form = {
    base: typed('setBase', state.base),
    key: typed('setKey', state.apiKey),
    user: typed('setUser', state.userId),
    transport: typed('setTransport', state.transport),
  };
  // 模型连接 / Embedding 那一块自己要求重画时，它已经把该留的值理好了（换协议、保存成功都要换掉表单里的值）
  if (fresh) {
    resetModelSection();
    resetEmbeddingSection();
  } else {
    if (!fromModel) captureModelForm();
    if (!fromEmbedding) captureEmbeddingForm();
  }
  // 整块重画会换掉滚动的那一层：记下滚到哪了，画完放回去，免得点一下就跳回顶上
  const scrolled = fresh ? 0 : (dialog.querySelector('.sheet') || {}).scrollTop || 0;
  setHtml(dialog, html`<div class="sheet" role="document">
    <div class="row-between">
      <h2 id="settingsTitle">${t('settings.title')}</h2>
      <button type="button" class="icon-btn" data-action="close" aria-label="${t('action.close')}" title="${t('action.close')}">${icon('close')}</button>
    </div>

    <div class="setting-row">
      <div><h3>${t('settings.language')}</h3></div>
      <div class="row" role="group" aria-label="${t('settings.language')}">
        <button type="button" class="btn btn-sm" data-action="lang" data-lang="en" aria-pressed="${String(state.lang === 'en')}">English</button>
        <button type="button" class="btn btn-sm" data-action="lang" data-lang="zh" aria-pressed="${String(state.lang === 'zh')}">中文</button>
      </div>
    </div>
    <div class="setting-row">
      <div><h3>${t('settings.learning')}</h3><p class="desc">${t('settings.learning.desc')}</p></div>
      ${switchButton('toggle-learning', state.learning, t('settings.learning'))}
    </div>

    ${isAdmin ? html`${modelSection()}${embeddingSection()}` : ''}

    <details class="disclosure" data-detail="advanced" ${advancedOpen ? 'open' : ''}>
      <summary>${t('settings.advanced')}</summary>
      <div class="disclosure-body">
        <form class="stack" data-form="connection">
          <h3>${t('settings.connection')}</h3>
          <div class="field"><label for="setBase">${t('settings.base')}</label>
            <input class="input" id="setBase" value="${form.base}" placeholder="${t('settings.base.placeholder')}" autocomplete="off" spellcheck="false">
            <span class="hint">${t('settings.base.hint')}</span></div>
          <div class="grid-2">
            <div class="field"><label for="setKey">${t('settings.key')}</label>
              <input class="input" id="setKey" type="password" value="${form.key}" autocomplete="off">
              <span class="hint">${t('settings.key.hint')}</span></div>
            <div class="field"><label for="setUser">${t('settings.user')}</label>
              <input class="input" id="setUser" value="${form.user}" autocomplete="off" spellcheck="false">
              <span class="hint">${t('settings.user.hint')}</span></div>
          </div>
          <div class="grid-2">
            <div class="field"><label for="setTransport">${t('settings.transport')}</label>
              <select class="select" id="setTransport">${['auto', 'query', 'stream'].map((item) => html`<option value="${item}"
                ${form.transport === item ? 'selected' : ''}>${t('settings.transport.' + item)}</option>`)}</select>
              <span class="hint">${t('settings.transport.hint')}</span></div>
            <div class="field"><span class="label">${t('settings.longTerm')}</span>
              ${switchButton('toggle-longterm', state.longTerm, t('settings.longTerm'))}
              <span class="hint">${t('settings.longTerm.hint')}</span></div>
          </div>
          <div><button type="submit" class="btn btn-primary">${t('settings.save')}</button></div>
        </form>

        ${isAdmin ? '' : modelSection()}

        <section class="stack">
          <h3>${t('settings.diagnostics')}</h3>
          <dl class="kv">${identityRows().map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>
          <div><button type="button" class="btn btn-sm" data-action="diagnose" ${diagnostics && diagnostics.status === 'running' ? 'disabled' : ''}>
            ${t('settings.diagnose')}</button></div>
          ${diagnostics ? html`<div role="status">${diagnostics.status === 'running'
            ? html`<div class="loading"><span class="spinner"></span>${t('settings.diagnosing')}</div>`
            : html`<dl class="kv">${diagnostics.rows.map(([label, value, tone]) => html`<dt>${label}</dt>
                <dd><span class="${tone === 'bad' ? 'chip chip-danger' : tone === 'ok' ? 'chip chip-ok' : 'mono'}">${value}</span></dd>`)}</dl>`}</div>` : ''}
        </section>

        <section class="stack">
          <h3>${t('settings.operations')}</h3>
          <p class="desc">${t(isAdmin ? 'settings.operations.desc' : 'settings.operations.denied')}</p>
          <div class="row">
            ${['schemaStatus', 'reloadSchema', 'resetCircuits', 'feedbackStats'].map((name) => html`<button type="button"
              class="btn btn-sm" data-action="operate" data-op="${name}"
              ${!isAdmin || (operation && operation.status === 'running') ? 'disabled' : ''}>${t('ops.' + name)}</button>`)}
          </div>
          ${operation ? html`<div role="status"><p class="muted">${t('ops.' + operation.name)} — ${t('ops.status.' + operation.status)}</p>
            ${operation.output ? html`<pre class="code">${operation.output}</pre>` : ''}</div>` : ''}
        </section>
      </div>
    </details>

    <section class="stack">
      <h3>${t('settings.keyboard')}</h3>
      <dl class="kv">
        <dt>${t('keys.send')}</dt><dd>Enter</dd>
        <dt>${t('keys.newline')}</dt><dd>Shift + Enter</dd>
        <dt>${t('keys.focus')}</dt><dd>/</dd>
        <dt>${t('keys.recall')}</dt><dd>↑</dd>
        <dt>${t('keys.close')}</dt><dd>Esc</dd>
      </dl>
    </section>
  </div>`);
  if (scrolled) dialog.querySelector('.sheet').scrollTop = scrolled;
}

async function diagnose() {
  diagnostics = { status: 'running', rows: [] };
  draw();
  const rows = [];
  try {
    await api.health();
    rows.push([t('diag.health'), t('diag.ok'), 'ok']);
  } catch (error) {
    rows.push([t('diag.health'), error.status ? `HTTP ${error.status}` : t('error.noResponse'), 'bad']);
  }
  let ready = null;
  try {
    ready = await api.ready();
  } catch (error) {
    // 未就绪时 /readyz 回 503，检查项在 detail 里
    ready = error.detail && typeof error.detail === 'object' ? error.detail : null;
    if (!ready) rows.push([t('diag.ready'), error.status ? `HTTP ${error.status}` : t('error.noResponse'), 'bad']);
  }
  if (ready) {
    rows.push([t('diag.ready'), ready.ready ? t('diag.ok') : t('diag.notReady'), ready.ready ? 'ok' : 'bad']);
    Object.entries(ready.checks || {}).forEach(([name, value]) => {
      rows.push([name, typeof value === 'string' ? value : JSON.stringify(value), '']);
    });
  }
  diagnostics = { status: 'done', rows };
  draw();
}

async function operate(name) {
  // 这两项会改服务端的状态，先问一句
  if ((name === 'reloadSchema' || name === 'resetCircuits') && !window.confirm(t('ops.confirm.' + name))) return;
  operation = { name, status: 'running', output: '' };
  draw();
  try {
    const payload = await api[name]();
    operation = { name, status: 'done', output: JSON.stringify(payload, null, 2) };
  } catch (error) {
    operation = { name, status: 'error', output: `HTTP ${error.status || 0}${error.message ? ' · ' + error.message : ''}` };
  }
  draw();
}

export function openSettings() {
  draw({ fresh: !dialog.open });
  if (!dialog.open) dialog.showModal();
  ensureModelLoaded();
}

export function initSettings() {
  dialog = $('dlgSettings');
  initModelSection(() => { if (dialog.open) draw({ fromModel: true }); }, embeddingStatusReady);
  initEmbeddingSection(() => { if (dialog.open) draw({ fromEmbedding: true }); });

  dialog.addEventListener('submit', (event) => {
    if (event.target.dataset.form === 'model') {
      event.preventDefault();
      modelActions['model-save']();
      return;
    }
    if (event.target.dataset.form === 'embedding') {
      event.preventDefault();
      embeddingActions['embedding-save']();
      return;
    }
    if (event.target.dataset.form !== 'connection') return;
    event.preventDefault();
    state.base = $('setBase').value.trim();
    state.apiKey = $('setKey').value.trim();
    state.userId = $('setUser').value.trim();
    state.transport = $('setTransport').value;
    save();
    diagnostics = null;
    // 换了 Key 或身份，模型连接那一块能不能看、能不能改都得重新问
    resetModelSection();
    resetEmbeddingSection();
    emit('reconnect');
    draw();
  });

  dialog.addEventListener('toggle', (event) => {
    const detail = event.target.dataset && event.target.dataset.detail;
    if (detail === 'model-connection') modelConnectionToggled(event.target.open);
    if (detail === 'embedding-connection') embeddingConnectionToggled(event.target.open);
    if (detail === 'advanced') advancedOpen = event.target.open;
  }, true);

  // 只有下拉框选了一项才重画。输入框离开时也会发 change，那时重画会把正要点的按钮换掉
  dialog.addEventListener('change', (event) => {
    if (event.target.tagName !== 'SELECT') return;
    if (event.target.id === 'modelProvider') modelProviderChanged(event.target.value);
    if (event.target.id === 'modelChat' || event.target.id === 'modelCoder') {
      modelPicked(event.target.id, event.target.value);
    }
    if (event.target.id === 'embModel') embeddingPicked(event.target.value);
  });
  dialog.addEventListener('input', (event) => {
    if (['modelBase', 'modelKey', 'modelChat', 'modelCoder'].includes(event.target.id)) modelTyped();
    if (['embBase', 'embKey', 'embDims', 'embModel'].includes(event.target.id)) embeddingTyped();
  });

  // 点到对话框外面的遮罩（事件目标是 dialog 本身）也算关闭
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });

  delegate(dialog, 'click', {
    close: () => dialog.close(),
    lang: (target) => {
      if (state.lang === target.dataset.lang) return;
      state.lang = target.dataset.lang;
      save();
      emit('lang');
    },
    'toggle-learning': () => {
      state.learning = !state.learning;
      save();
      emit('learning');
    },
    'toggle-longterm': () => {
      state.longTerm = !state.longTerm;
      save();
      draw();
    },
    diagnose: () => diagnose(),
    operate: (target) => operate(target.dataset.op),
    ...modelActions,
    ...embeddingActions,
  });

  on('open-settings', openSettings);
  on('lang', () => { if (dialog.open) draw(); });
  on('learning', () => { if (dialog.open) draw(); });
  // 身份是跟着目录一起回来的：对话框开着的话，这时候才知道能不能去取模型连接
  on('catalog', () => {
    if (!dialog.open) return;
    draw();
    ensureModelLoaded();
  });
}
