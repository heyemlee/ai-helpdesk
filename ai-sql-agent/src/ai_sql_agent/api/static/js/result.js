/* 一轮问答里「答案」那一块。

   从上到下：状态（一行）→ 结论 → 需要人注意的提示 → 每一步结果一块面板 →
   数据日期 → 操作与反馈 → 查询过程（普通模式默认收起；学习模式换成逐步讲解）。

   结果面板有三种看法，是同一批行的三种画法，互相切换不丢东西：
   只有一行时是几个大数字；一列名字加一列数字时是一张图；任何时候都能看表格。

   页面不显示服务端 answer 字段里那串「已完成数据库查询。查询结果：[JSON]」——
   那是给下游程序留的文本。这里的结论由结构化字段（行数、列、单位）直接拼出来：
   能拼出什么就说什么，拼不出来就只报行数，不编一句话。 */

import { domainTitleOf } from './catalog.js';
import { html, icon } from './dom.js';
import {
  descriptionOf, formatCell, formatDuration, formatRange, formatTime, recordDates, toTsv,
} from './format.js';
import { has, t, tn } from './i18n.js';
import { chartFor, stepViews } from './insight.js';
import { classify, splitSteps } from './model.js';
import { renderLearning, renderProcess, renderStageTrack } from './process.js';
import { renderRichText } from './richtext.js';
import { state } from './state.js';
import {
  anomalyHelp, anomalyLabel, failureReason, notice, sourceLabel, stepColumns, stepMatrix,
} from './steps.js';
import { renderPager, renderTable, sortRows } from './table.js';
import { databaseSlot, dbTile } from './views/exploreParts.js';

const PAGE_SIZE = 25;
const MAX_FACTS = 8;
const MAX_NAMES = 5;
const VIEW_ICONS = { figures: 'hash', chart: 'chart', table: 'table' };
// 「对 / 不对」只画拇指（名字在 aria-label 和悬停提示里），「不确定」没有公认的图标，写出字来
const VERDICTS = [['correct', 'thumbUp', false], ['wrong', 'thumbDown', false], ['unsure', 'help', true]];

function tableState(turn, step) {
  turn.ui.tables[step.index] = turn.ui.tables[step.index] || { page: 1, sort: null };
  return turn.ui.tables[step.index];
}

/* 结论：{ headline, facts, names }。全部来自这一轮返回的行。 */
function conclusion(result, view, answers) {
  if (view.kind === 'no_data') return { headline: t('result.noData'), facts: [], names: '' };
  if (view.kind === 'failed') return { headline: t('result.failed'), facts: [], names: '' };

  const rowTotal = answers.reduce((sum, step) => sum + step.rowCount, 0);
  const facts = [];
  if (answers.every((step) => step.rowCount === 1)) {
    answers.forEach((step) => {
      const columns = stepColumns(step);
      columns.forEach((column) => {
        const cell = formatCell(step.rows[0][column.key], { unit: column.unit, plain: column.plain });
        facts.push({ label: column.label, value: cell.text, unit: cell.empty ? '' : column.unit });
      });
    });
  }

  let names = '';
  const last = answers[answers.length - 1];
  if (last && answers.length === 1 && last.rowCount > 1) {
    const columns = stepColumns(last);
    const naming = columns.find((column) => column.entry && column.entry.role === 'output_dimension')
      || columns.find((column) => /(^|_)name$/i.test(column.key));
    if (naming) {
      const values = [...new Set(last.rows.map((row) => row[naming.key]).filter((value) => value != null))];
      // 名字本身带逗号时（「3/4 in panel, 4 x 8 ft」）换成分号分隔，否则数不清有几个
      const separator = values.some((value) => String(value).includes(',')) ? '; ' : ', ';
      names = values.slice(0, MAX_NAMES).join(separator);
      if (values.length > MAX_NAMES) names += t('result.andMore', { n: values.length - MAX_NAMES });
    }
  }

  let headline;
  if (view.kind === 'partial') {
    headline = t('result.partial', { ok: result.steps.length - view.failedSteps.length, total: result.steps.length });
  } else if (answers.length > 1) {
    headline = t('result.parts', { n: answers.length, rows: rowTotal });
  } else if (view.truncated) {
    headline = t('result.recordsTruncated', { n: rowTotal });
  } else {
    headline = rowTotal === 1 ? t('result.oneRecord') : t('result.records', { n: rowTotal });
  }
  return { headline, facts: facts.length <= MAX_FACTS ? facts : [], names };
}

/* 复制用的纯文本结论。 */
export function summaryText(turn) {
  const result = turn.result;
  const view = classify(result);
  // 复制出去的文字会离开这个页面单独流传，页面上的提示必须跟着走：
  // 少一句「结果被截断」或「来自降级链路」，贴到邮件里就成了一个不带条件的结论。
  const caveats = [];
  if (view.degraded) caveats.push(t('notice.degraded.title'));
  if (view.kind === 'conversation') {
    return [turn.question, '', result.answer, '', t('notice.conversation.title'), ...caveats].join('\n');
  }
  const { answers } = splitSteps(result, turn.question);
  const summary = conclusion(result, view, answers);
  const lines = [turn.question, '', summary.headline + (summary.names ? `: ${summary.names}` : '')];
  summary.facts.forEach((fact) => {
    lines.push(`${fact.label}: ${fact.value}${fact.unit ? ' ' + fact.unit : ''}`);
  });
  const sources = [...new Set(answers.map((step) => sourceLabel(step.database)))];
  if (sources.length) lines.push('', `${t('meta.source')}: ${sources.join(', ')}`);
  if (result.asOf) lines.push(`${t('meta.readAtPlain')}: ${result.asOf}`);
  if (view.kind === 'partial') caveats.unshift(t('notice.partial.title'));
  if (view.truncated) caveats.unshift(t('notice.truncated.title'));
  if (view.needsReview) caveats.unshift(t('notice.review.title'));
  if (caveats.length) lines.push('', ...caveats);
  return lines.join('\n');
}

/* 这一轮有了结果时念给读屏的那句话：状态加结论，页面上最先该知道的那两样。 */
export function announcement(turn) {
  if (turn.status === 'stopped') return t('notice.stopped.title');
  if (turn.status === 'error' || !turn.result) return t('notice.service.title');
  const view = classify(turn.result);
  if (view.kind === 'conversation') return t('status.conversation');
  const { answers } = splitSteps(turn.result, turn.question);
  const summary = conclusion(turn.result, view, answers);
  const parts = [summary.headline + (summary.names ? `: ${summary.names}` : '')];
  if (view.needsReview) parts.push(t('status.review'));
  if (view.truncated) parts.push(t('status.truncated'));
  if (view.degraded) parts.push(t('status.degraded'));
  return parts.join('. ');
}

export function tableText(turn, stepIndex) {
  const step = turn.result.steps.find((item) => item.index === stepIndex);
  if (!step) return '';
  const columns = stepColumns(step);
  return toTsv(
    columns.map((column) => (column.unit ? `${column.label} (${column.unit})` : column.label)),
    stepMatrix(step, columns),
  );
}

/* 答案最上面那一行：标记 + 状态，右边是耗时（或者等待时的计时与停止）。 */
function answerHead(status, aside = '') {
  return html`<div class="answer-head">
    <span class="agent-mark" aria-hidden="true">${icon('logo')}</span>
    <div class="status-line">${status}</div>
    ${aside}
  </div>`;
}

/* 状态标签。每种状态都是「图标 + 文字」，颜色只是第三重提示。 */
function statusChips(view) {
  const map = {
    success: ['chip-ok', 'check', 'status.success'],
    no_data: ['chip-info', 'empty', 'status.no_data'],
    partial: ['chip-warn', 'warn', 'status.partial'],
    failed: ['chip-danger', 'error', 'status.failed'],
    conversation: ['', 'ask', 'status.conversation'],
  };
  const [tone, glyph, key] = map[view.kind];
  return html`<span class="chip ${tone}">${icon(glyph, 'icon-sm')}${t(key)}</span>
    ${view.needsReview ? html`<span class="chip chip-warn">${icon('warn', 'icon-sm')}${t('status.review')}</span>` : ''}
    ${view.truncated ? html`<span class="chip chip-warn">${t('status.truncated')}</span>` : ''}
    ${view.degraded ? html`<span class="chip chip-warn">${t('status.degraded')}</span>` : ''}`;
}

/* 这一轮花了多久。显示的是页面等到结果的时间；服务端自己记的那一段在悬停提示里。 */
function elapsed(result) {
  if (result.clientMs == null) return '';
  const hint = t('process.clientTime', { time: formatDuration(result.clientMs) })
    + (result.serverMs != null ? ` · ${t('process.serverTime', { time: formatDuration(result.serverMs) })}` : '');
  return html`<span class="answer-time" title="${hint}">${icon('clock', 'icon-sm')}${formatDuration(result.clientMs)}</span>`;
}

function notices(result, view) {
  const out = [];

  if (view.kind === 'failed') {
    const reasons = view.failedSteps.map((step) => failureReason(step.error));
    const first = reasons[0] || { key: result.degraded ? 'fail.modelDown' : 'fail.generic', detail: '' };
    out.push(notice('danger', 'error', t('notice.failed.title'), html`
      <p>${t(first.key)}${first.detail ? html` <span class="mono">${first.detail}</span>` : ''}</p>
      <p>${t('notice.failed.hint')}</p>`));
  }

  if (view.kind === 'no_data') {
    const notes = view.findings.filter((finding) => finding.anomaly === 'empty_result');
    out.push(notice('info', 'empty', t('notice.noData.title'), html`
      <p>${t('notice.noData.body')}</p>
      ${notes.map((finding) => html`<p class="original" lang="zh">${t('notice.validatorNote')} ${finding.detail}</p>`)}`));
  }

  if (view.kind === 'partial') {
    out.push(notice('warn', 'warn', t('notice.partial.title'), html`
      <p>${t('notice.partial.body')}</p>
      <ul>${view.failedSteps.map((step) => {
        const reason = failureReason(step.error);
        return html`<li>${t('process.step', { n: step.index + 1 })} · ${sourceLabel(step.database)}: ${t(reason.key)}
          ${reason.detail ? html`<span class="mono">${reason.detail}</span>` : ''}</li>`;
      })}</ul>`));
  }

  if (view.needsReview) {
    const findings = view.findings.filter((finding) => !(view.kind === 'no_data' && finding.anomaly === 'empty_result'));
    out.push(notice('warn', 'warn', t('notice.review.title'), html`
      <p>${t('notice.review.body')}</p>
      ${findings.length ? html`<ul>${findings.map((finding) => html`<li>
          <strong>${anomalyLabel(finding.anomaly)}</strong>${finding.severity === 'block' ? ` · ${t('notice.review.block')}` : ''}
          ${anomalyHelp(finding.anomaly) ? html` — ${anomalyHelp(finding.anomaly)}` : ''}
          ${finding.detail ? html`<br><span class="original" lang="zh">${t('notice.validatorNote')} ${finding.detail}</span>` : ''}
        </li>`)}</ul>` : html`<p>${t('notice.review.unspecified')}</p>`}`));
  }

  if (view.truncated) {
    const limits = [...new Set(result.steps.filter((step) => step.truncated && step.rowLimit).map((step) => step.rowLimit))];
    out.push(notice('warn', 'warn', t('notice.truncated.title'), html`<p>${limits.length
      ? t('notice.truncated.body', { limit: limits.join(' / ') }) : t('notice.truncated.bodyNoLimit')}</p>`));
  }

  if (view.degraded && view.kind !== 'failed') {
    out.push(notice('warn', 'info', t('notice.degraded.title'), html`<p>${t('notice.degraded.body')}</p>`));
  }
  return out;
}

/* ─── 结果面板的三种看法 ────────────────────────────────────────── */

/* 只有一行：每一列一个大数字。文字列（名字、状态）字号小一档，免得一长串字撑满一行。 */
function figuresView(step, columns) {
  const row = step.rows[0];
  return html`<dl class="stat-tiles">${columns.map((column) => {
    const cell = formatCell(row[column.key], { unit: column.unit, plain: column.plain });
    return html`<div class="stat-tile">
      <dt class="stat-label" ${column.hint ? html`title="${column.hint}"` : ''}>${column.label}</dt>
      <dd class="stat-value${cell.numeric ? '' : ' is-text'}${cell.empty ? ' is-empty' : ''}">${cell.text}${column.unit && !cell.empty
        ? html`<span class="stat-unit">${column.unit}</span>` : ''}</dd>
    </div>`;
  })}</dl>`;
}

/* 一段文字大概占几个字符宽：中日韩文字算两个。只用来给图里的名字列留宽度。 */
const textWidth = (text) => [...text].reduce((sum, char) => sum + (/[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]/.test(char) ? 2 : 1), 0);

/* 一列名字加一列数字：横条，或者（连着的年月）一排柱子。画的就是返回的那几行，
   条的颜色跟着来源库的标识色走。

   横条把每一行的名字和数字都写在条的旁边，单独看不缺东西。柱子上写不下数字（悬停提示
   在手机上、用键盘时都看不到），所以柱子只放在表格上面，不单独当一种看法。 */
function chartView(chart) {
  const peak = Math.max(...chart.items.map((item) => item.value));
  const show = (amount) => formatCell(amount, { unit: chart.value.unit }).text;
  const title = t('chart.caption', { value: chart.value.label, label: chart.label.label })
    + (chart.value.unit ? ` (${chart.value.unit})` : '');

  if (chart.kind === 'columns') {
    // 最高的可能不止一个：并列的都写出来，太多就不写这句（下面的表格里都有）
    const tops = chart.items.filter((item) => item.value === peak);
    const first = chart.items[0];
    const last = chart.items[chart.items.length - 1];
    return html`<div class="answer-chart is-above-table">
      <p class="chart-caption">${title}</p>
      <div class="viz-cols is-static" role="img" aria-label="${title}: ${first.text} – ${last.text}">${chart.items.map((item) => html`<span
        class="viz-col" title="${item.text} · ${show(item.value)}"><i style="height:${item.value ? Math.max(3, (item.value / peak) * 100) : 0}%"></i></span>`)}</div>
      <div class="viz-axis" aria-hidden="true"><span>${first.text}</span><span>${last.text}</span></div>
      ${tops.length <= 3 ? html`<p class="chart-note">${t('chart.peak', { label: tops.map((item) => item.text).join(', '), value: show(peak) })}</p>` : ''}
    </div>`;
  }

  // 名字、数字这两列各行一样宽，条才对得齐：宽度按最长的那个名字、那个数字算
  const nameWidth = Math.min(30, Math.max(6, ...chart.items.map((item) => textWidth(item.text))));
  const valueWidth = Math.max(...chart.items.map((item) => show(item.value).length));
  return html`<div class="answer-chart">
    <p class="chart-caption">${title}</p>
    <ul class="viz-list has-bars" style="--name-ch:${nameWidth};--value-ch:${valueWidth}">${chart.items.map((item) => html`<li><span class="viz-row is-static">
      <span class="viz-name" title="${item.text}">${item.text}</span>
      <span class="viz-track" aria-hidden="true"><i style="width:${item.value ? Math.max(1.5, (item.value / peak) * 100) : 0}%"></i></span>
      <span class="viz-count">${show(item.value)}</span>
    </span></li>`)}</ul>
  </div>`;
}

function tableView(turn, step, columns, label) {
  const ui = tableState(turn, step);
  let rows = stepMatrix(step, columns);
  if (ui.sort) {
    const index = columns.findIndex((column) => column.key === ui.sort.key);
    if (index >= 0) rows = sortRows(rows, index, ui.sort.direction);
  }
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const page = Math.min(Math.max(1, ui.page), pageCount);
  const from = (page - 1) * PAGE_SIZE;
  const shown = rows.slice(from, from + PAGE_SIZE);

  return html`${renderTable({ columns, rows: shown, sort: ui.sort, sortAction: 'sort-table', scope: step.index, label })}
    ${rows.length > PAGE_SIZE || step.truncated ? html`<div class="table-foot">
      <span>${rows.length > PAGE_SIZE
        ? t('table.range', { from: from + 1, to: from + shown.length, total: rows.length })
        : tn('result.rows', rows.length)}${step.truncated ? ` · ${t('table.truncatedRows')}` : ''}${ui.sort && step.truncated
        ? ` · ${t('table.sortedPartial')}` : ''}</span>
      ${renderPager({ page, pageCount, action: 'page-table', scope: step.index })}
    </div>` : ''}`;
}

/* 这些列是什么意思（数据字典里的说明）。哪种看法下都在面板最下面：
   看的是大数字或图的时候，同样需要知道那个数是怎么定义的。 */
function legend(turn, step, columns) {
  if (!columns.some((column) => column.entry)) return '';
  return html`<details class="disclosure is-legend" data-detail="legend-${step.index}" ${turn.ui.open['legend-' + step.index] ? 'open' : ''}>
    <summary>${t('result.legend')}</summary>
    <div class="disclosure-body"><dl class="kv">${columns.map((column) => {
      const description = column.entry ? descriptionOf(column.entry) : null;
      return html`<dt>${column.label}${column.unit ? ` (${column.unit})` : ''}<br><span class="tech">${column.key}</span></dt>
        <dd>${description && description.text
          ? html`<span ${description.original ? html`lang="zh"` : ''}>${description.text}</span>`
          : html`<span class="muted">${column.entry ? t('data.noDescription') : t('result.computedColumn')}</span>`}</dd>`;
    })}</dl><p class="muted">${t('result.legendNote')}</p></div>
  </details>`;
}

/* 一步结果的面板：来自哪个库、多少行，怎么看由面板头上的开关定（记在 turn.ui.views 上）。 */
function stepPanel(turn, step, multiple) {
  const columns = stepColumns(step);
  const views = stepViews(step, columns, MAX_FACTS);
  const chosen = turn.ui.views[step.index];
  const view = views.includes(chosen) ? chosen : views[0];
  const chart = chartFor(step, columns);
  const title = domainTitleOf(step.database);
  const label = `${sourceLabel(step.database)} — ${tn('result.rows', step.rowCount)}`;

  return html`<section class="result-panel db-c${databaseSlot(step.database)}" aria-label="${label}">
    <div class="result-panel-head">
      <span class="result-panel-title">${dbTile(step.database, 'is-sm')}
        <span>${multiple ? `${t('process.step', { n: step.index + 1 })} · ` : ''}${title}</span>
        ${title !== step.database ? html`<span class="tech">${step.database}</span>` : ''}</span>
      <span class="result-panel-count">${tn('result.rows', step.rowCount)}</span>
      <span class="result-panel-tools">
        ${views.length > 1 ? html`<span class="seg" role="group" aria-label="${t('view.switch')}">${views.map((name) => html`<button
          type="button" data-action="step-view" data-step="${step.index}" data-which="${name}"
          aria-pressed="${String(name === view)}">${icon(VIEW_ICONS[name], 'icon-sm')}${t('view.' + name)}</button>`)}</span>` : ''}
        <button type="button" class="act" data-action="copy-table" data-step="${step.index}" title="${t('action.copyTable')}">
          ${icon('copy', 'icon-sm')}<span class="act-label">${t('action.copyTable')}</span></button>
      </span>
    </div>
    ${view === 'figures' ? figuresView(step, columns)
      : view === 'chart' ? chartView(chart)
        : html`${chart && chart.kind === 'columns' ? chartView(chart) : ''}${tableView(turn, step, columns, label)}`}
    ${legend(turn, step, columns)}
  </section>`;
}

/* 这批数据是什么时候的。来源库写在每块面板的头上；有哪一步没有面板（它没有返回行）时，
   在这里把来源列全——查过的库不能因为没查到东西就从页面上消失。 */
function metaLine(result, answers, withSources) {
  const sources = [...new Set(answers.map((step) => sourceLabel(step.database)))];
  const dates = answers.flatMap((step) => {
    const columns = stepColumns(step);
    return recordDates(columns, step.rows, (row, column) => row[column.key])
      .map((range) => `${formatRange(range)} (${range.column.label})`);
  });
  const read = !result.asOf
    ? t('meta.readUnknown')
    : result.asOfSource === 'query_time'
      ? t('meta.readLive', { time: formatTime(result.asOf) })
      : result.asOfSource === 'snapshot'
        ? t('meta.readSnapshot', { time: formatTime(result.asOf) })
        : t('meta.readUnknown');

  return html`<div class="result-meta">
    ${withSources && sources.length ? html`<span><strong>${t('meta.source')}:</strong> ${sources.join(', ')}</span>` : ''}
    <span><strong>${t('meta.recordDates')}:</strong> ${dates.length ? dates.join('; ') : t('meta.noRecordDates')}</span>
    <span title="${t('meta.readHint')}">${read}</span>
  </div>`;
}

/* 反馈：{ inline, extra }。inline 和操作按钮排在同一行；补充说明的输入框与报错另起一行。 */
function feedback(turn) {
  const traceId = turn.result.traceId;
  if (!traceId || (state.service && state.service.feedback_enabled === false)) return { inline: '', extra: '' };
  const sent = state.feedback[traceId];
  const ui = turn.ui.feedback;
  if (sent) {
    return {
      inline: html`<div class="feedback is-sent" role="status">${icon('check', 'icon-sm')}<span>${t('feedback.sent.' + sent)}</span>
        <button type="button" class="link-btn" data-action="feedback-reset">${t('feedback.change')}</button></div>`,
      extra: '',
    };
  }
  return {
    inline: html`<div class="feedback">
      <span>${t('feedback.ask')}</span>
      ${VERDICTS.map(([verdict, glyph, labelled]) => html`<button type="button" class="vote${labelled ? '' : ' is-icon'}"
        data-action="feedback" data-verdict="${verdict}" title="${t('feedback.' + verdict)}"
        ${labelled ? '' : html`aria-label="${t('feedback.' + verdict)}"`} ${ui.sending ? 'disabled' : ''}>${labelled
        ? html`${icon(glyph, 'icon-sm')}${t('feedback.' + verdict)}` : icon(glyph)}</button>`)}
      <button type="button" class="link-btn" data-action="feedback-comment" aria-expanded="${ui.open ? 'true' : 'false'}">${t('feedback.addComment')}</button>
    </div>`,
    extra: html`${ui.open ? html`<textarea class="textarea" rows="2" data-input="feedback-comment" maxlength="2000"
        aria-label="${t('feedback.commentLabel')}" placeholder="${t('feedback.commentPlaceholder')}">${ui.comment}</textarea>` : ''}
      ${ui.error ? html`<span class="chip chip-danger chip-wrap" role="alert">${t('feedback.failed')}: ${ui.error}</span>` : ''}`,
  };
}

function actions(view, turn) {
  // 历史页回看缓存结果时不放「再问一次 / 追问」：那一页有自己的「作为新查询重新执行」，
  // 两个长得像的按钮放在一起，人分不清哪个会去读最新数据。
  return html`<div class="answer-actions">
    <button type="button" class="act" data-action="copy-summary">${icon('copy', 'icon-sm')}${t('action.copyAnswer')}</button>
    ${turn.cached ? '' : html`<button type="button" class="act" data-action="ask-again">${icon('refresh', 'icon-sm')}${t('action.askAgain')}</button>`}
    ${!turn.cached && view.kind !== 'failed' ? html`<button type="button" class="act" data-action="follow-up">${icon('reply', 'icon-sm')}${t('action.followUp')}</button>` : ''}
  </div>`;
}

/* 这一轮的来源标记：历史回看、重新执行。旧结果不能看起来像新的。 */
function originNotice(turn) {
  if (turn.cached) {
    return notice('info', 'history', t('origin.cached.title'), html`<p>${t('origin.cached.body', {
      time: formatTime(turn.result.receivedAt),
    })}</p>`);
  }
  if (turn.rerunOf) {
    // 「刚读出来的」只在这次真的查了库时才说
    const key = turn.result.queriedDatabase ? 'origin.rerun.body' : 'origin.rerun.bodyNoQuery';
    return notice('info', 'refresh', t('origin.rerun.title'), html`<p>${t(key, { time: turn.rerunOf })}</p>`);
  }
  return '';
}

/* 等结果时那句「现在在做什么」。有阶段事件就照事件说，没有就只说在处理。 */
export function pendingStage(turn) {
  const stage = turn.stageNow;
  return stage && has('stage.' + stage.stage)
    ? t('stage.' + stage.stage) + (stage.database ? ` (${domainTitleOf(stage.database)})` : '')
    : t('ask.working');
}

/* 等待中。走流式时有六段进度（真实的阶段事件）；走 POST /query 时服务端什么都不回报，
   只有一条不表示进度的动效——不拿定时器假装在一步步往前走。 */
export function renderPending(turn, elapsedSeconds) {
  const streamed = turn.transport === 'a2a-stream';
  return html`<div class="answer is-pending">
    <div class="answer-head">
      <span class="agent-mark is-busy" aria-hidden="true">${icon('logo')}</span>
      <span class="pending-text" role="status" data-role="stage-text">${pendingStage(turn)}</span>
      <span class="answer-time" data-role="elapsed" aria-live="off">${elapsedSeconds >= 2 ? t('ask.elapsed', { n: elapsedSeconds }) : ''}</span>
      <button type="button" class="act" data-action="stop">${icon('stop', 'icon-sm')}${t('action.stop')}</button>
    </div>
    <div class="answer-body">
      ${streamed
        ? html`<div data-role="stage-track">${renderStageTrack(turn.stages)}</div>`
        : html`<div class="progress-indet" aria-hidden="true"></div>`}
      <div class="skeleton" aria-hidden="true"><i></i><i></i><i></i></div>
      ${state.learning ? html`<div data-role="learn-slot">${renderLearning(turn)}</div>` : ''}
    </div>
  </div>`;
}

/* 停止之后说清楚服务端那边怎样了。流式请求停止时会显式请服务端取消，turn.cancel 记着结果：
   请求还在路上或已受理、服务端已经跑完（409）。其余情况（/query 没有运行号、取消请求本身失败）
   只能靠断开连接：服务端默认在断开后停下（AI_SQL_CANCEL_ON_DISCONNECT），但这个开关可以关。 */
const STOPPED_BODY = { sent: 'notice.cancelled.body', accepted: 'notice.cancelled.body', finished: 'notice.cancelLate.body' };

export function renderStopped(turn) {
  const body = STOPPED_BODY[turn && turn.cancel] || 'notice.stopped.body';
  return html`<div class="answer">
    ${answerHead(html`<span class="chip">${icon('stop', 'icon-sm')}${t('status.stopped')}</span>`)}
    <div class="answer-body">
      ${notice('info', 'info', t('notice.stopped.title'), html`<p>${t(body)}</p>`)}
      <div class="answer-foot"><div class="answer-actions"><button type="button" class="act" data-action="ask-again">
        ${icon('refresh', 'icon-sm')}${t('action.askAgain')}</button></div></div>
    </div>
  </div>`;
}

/* 请求本身没成功（没连上、被拒、服务端出错）——和「查询跑了但失败」是两回事。 */
export function renderServiceError(turn) {
  const error = turn.error || {};
  const status = error.status || 0;
  let key = 'error.generic';
  if (error.code === 'stream_interrupted') key = 'error.interrupted';
  else if (error.code === 'bad_response') key = 'error.badResponse';
  else if (status === 0) key = 'error.network';
  else if (status === 401) key = 'error.401';
  else if (status === 403) key = 'error.403';
  else if (status === 429) key = 'error.429';
  else if (status === 400 || status === 422) key = 'error.400';
  else if (status >= 500) key = 'error.5xx';
  const needsSettings = status === 0 || status === 401 || status === 403;

  return html`<div class="answer">
    ${answerHead(html`<span class="chip chip-danger">${icon('error', 'icon-sm')}${t('status.error')}</span>`)}
    <div class="answer-body">
      ${notice('danger', 'error', t('notice.service.title'), html`
        <p>${t(key)}</p>
        <p class="original">${status ? `HTTP ${status}` : t('error.noResponse')}${error.serverMessage
          ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
      <div class="answer-foot"><div class="answer-actions">
        <button type="button" class="act" data-action="ask-again">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
        ${needsSettings ? html`<button type="button" class="act" data-action="open-settings">${icon('settings', 'icon-sm')}${t('action.checkSettings')}</button>` : ''}
      </div></div>
      ${state.learning ? renderLearning(turn) : ''}
    </div>
  </div>`;
}

export function renderResult(turn) {
  const result = turn.result;
  const view = classify(result);
  const { answers } = splitSteps(result, turn.question);
  const vote = feedback(turn);
  let content;

  if (view.kind === 'conversation') {
    content = html`${renderRichText(result.answer)}
      ${notice('info', 'info', t('notice.conversation.title'), html`<p>${t('notice.conversation.body')}</p>`)}
      ${view.degraded ? notice('warn', 'info', t('notice.degraded.title'), html`<p>${t('notice.degradedChat.body')}</p>`) : ''}`;
  } else {
    const summary = conclusion(result, view, answers);
    const showTables = view.kind === 'success' || view.kind === 'partial';
    const panels = showTables ? answers.filter((step) => step.rowCount > 0) : [];
    content = html`<p class="conclusion">${summary.headline}${summary.names ? html`: <strong>${summary.names}</strong>` : ''}
        ${result.currency && showTables ? html`<span class="chip" title="${t('meta.currencyHint')}">${t('meta.currency', { currency: result.currency })}</span>` : ''}</p>
      ${notices(result, view)}
      ${panels.map((step) => stepPanel(turn, step, panels.length > 1))}
      ${view.kind !== 'failed' ? metaLine(result, answers, panels.length !== answers.length) : ''}`;
  }

  return html`<div class="answer">
    ${answerHead(statusChips(view), elapsed(result))}
    <div class="answer-body">
      ${originNotice(turn)}
      ${content}
      <div class="answer-foot">${actions(view, turn)}${vote.inline}</div>
      ${vote.extra}
      ${state.learning ? renderLearning(turn) : renderProcess(turn)}
    </div>
  </div>`;
}
