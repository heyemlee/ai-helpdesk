/* 查询过程（普通模式，默认收起）与学习模式的逐步讲解。

   两者展示的是同一批事实，只是讲法不同。**事实只有两个来源**：

   1. 这一轮响应里的字段（route、steps[].final_sql、repair_attempts、risk_findings…）；
   2. 走 A2A 流式时，服务端真实发出的阶段事件（哪个阶段、第几步、哪个库、相对时刻）。

   没有第三个来源。响应里看不出来的事（比如这次有没有单独调规划模型），
   在没有阶段事件时就写「这条路径不回报」，不按「通常会这样」补一句。
   每一步后面那段讲解说的是设计，不是这次请求的经过，两者在版面上分开。 */

import { html, icon } from './dom.js';
import { formatDuration, formatTime } from './format.js';
import { has, t, tn } from './i18n.js';
import { classify, passedOn, splitSteps } from './model.js';
import { anomalyLabel, sourceLabel, stepColumns, stepMatrix } from './steps.js';
import { renderTable } from './table.js';

const RAW_LIMIT = 30000;
const INTERMEDIATE_ROWS = 10;

// 每一步讲解对应的源码位置（相对仓库根）。路径不翻译。
const SOURCES = {
  understand: 'src/ai_sql_agent/pipeline/routing.py · pipeline/dynamic_service.py · normalization/normalizer.py',
  retrieve: 'src/ai_sql_agent/retrieval/hierarchical_service.py · retrieval/rrf_fusion_client.py · pipeline/schema_budget.py · retrieval/access.py',
  plan: 'src/ai_sql_agent/planning/cot_planner.py · pipeline/short_path.py · pipeline/text2sql_pipeline.py (_as_sql_literals)',
  generate: 'src/ai_sql_agent/generation/sql_generator.py · generation/prompt_builder.py · execution/dialects.py',
  execute: 'src/ai_sql_agent/execution/sqlite_executor.py · execution/sql_guard.py · auth/enforcement.py · repair/repairer.py',
  check: 'src/ai_sql_agent/validation/validator.py · validation/cardinality.py · validation/self_check.py · api/result_summary.py · api/trust.py',
};

// 契约 §4.5 的阶段 → 学习模式的六步
const STAGE_TO_STEP = {
  normalization: 'understand', routing: 'understand', answering: 'understand',
  retrieval: 'retrieve', planning: 'plan', generation: 'generate',
  execution: 'execute', repair: 'execute', validation: 'check',
};

const STEP_ORDER = ['understand', 'retrieve', 'plan', 'generate', 'execute', 'check'];

/* 等结果时的六段进度：每一步现在是 done / now / todo。

   只看服务端真实发来的阶段事件。最后一条事件所在的那一步是「正在做」；排在它前面的是
   「过去了」——服务端按顺序一个阶段一个阶段地走，后面的阶段开始了，前面的就已经结束。
   前面某一步没收到事件不代表它被跳过：进度事件在队列满的时候会被丢掉（终态不会），
   而规划这一步服务端每次都会报（简单问题只是带一个 short_path 标记）。
   没有阶段事件（POST /query 这条路径）就画不出这条进度，调用方不该调它。 */
export function stageProgress(stages) {
  const steps = (stages || []).map((stage) => STAGE_TO_STEP[stage.stage]).filter(Boolean);
  const current = steps.length ? STEP_ORDER.indexOf(steps[steps.length - 1]) : -1;
  return STEP_ORDER.map((id, index) => ({
    id,
    state: index === current ? 'now' : index < current ? 'done' : 'todo',
  }));
}

/* 进度条本身不念给读屏：当前阶段已经由旁边那句状态文字（role="status"）说了。 */
export function renderStageTrack(stages) {
  return html`<ol class="stage-track" aria-hidden="true">${stageProgress(stages).map((step) => html`<li
    class="stage is-${step.state}">${t('step.' + step.id)}</li>`)}</ol>`;
}

const routeLabel = (route) => (has('route.' + route) ? `${t('route.' + route)} (${route})` : route || '—');
const transportLabel = (transport) => t(transport === 'a2a-stream' ? 'process.transport.stream' : 'process.transport.query');
const offset = (stage) => (stage.offsetMs == null ? '' : `+${(stage.offsetMs / 1000).toFixed(2)}s`);

function sqlBlock(sql, stepIndex, which) {
  return html`<div class="row-between"><span class="muted">${t('process.sql.' + which)}</span>
      <button type="button" class="btn btn-sm btn-ghost" data-action="copy-sql" data-step="${stepIndex}" data-which="${which}">
        ${icon('copy', 'icon-sm')}${t('action.copySql')}</button></div>
    <pre class="code">${sql}</pre>`;
}

function stepChips(step, intermediate) {
  return html`${step.success
      ? html`<span class="chip">${tn('result.rows', step.rowCount)}</span>`
      : html`<span class="chip chip-danger">${t('process.stepFailed')}</span>`}
    ${step.truncated ? html`<span class="chip chip-warn">${t('status.truncated')}${step.rowLimit ? ` · ${step.rowLimit}` : ''}</span>` : ''}
    ${step.repairs.length ? html`<span class="chip chip-warn">${t('process.repaired', { n: step.repairs.length })}</span>` : ''}
    ${step.findings.length ? html`<span class="chip chip-warn">${tn('process.findings', step.findings.length)}</span>` : ''}
    ${intermediate ? html`<span class="chip chip-info">${t('process.intermediate')}</span>` : ''}`;
}

function repairList(step) {
  if (!step.repairs.length) return '';
  return html`<div><p class="muted">${t('process.repairs')}</p><ol class="stack">${step.repairs.map((attempt) => html`<li>
    <span class="chip ${attempt.success ? '' : 'chip-danger'}">#${attempt.attempt_no ?? ''} ${attempt.success ? t('process.repairOk') : t('process.repairFailed')}</span>
    <span class="tech">${attempt.error_type || ''}${attempt.failing_clause ? ` · ${attempt.failing_clause}` : ''}${attempt.strategy ? ` · ${attempt.strategy}` : ''}</span>
    ${attempt.error_after_repair ? html`<p class="notice-body original" lang="zh">${attempt.error_after_repair}</p>` : ''}
  </li>`)}</ol></div>`;
}

function findingList(step) {
  if (!step.findings.length) return '';
  return html`<ul class="stack">${step.findings.map((finding) => html`<li>
    <span class="chip chip-warn">${anomalyLabel(finding.anomaly)}</span>
    <span class="tech">${finding.anomaly}${finding.severity ? ` · ${finding.severity}` : ''}</span>
    ${finding.detail ? html`<p lang="zh">${finding.detail}</p>` : ''}
  </li>`)}</ul>`;
}

function intermediateRows(step) {
  if (!step.rows.length) return '';
  const columns = stepColumns(step);
  const rows = stepMatrix(step, columns).slice(0, INTERMEDIATE_ROWS);
  return html`<div>${renderTable({ columns, rows, label: t('process.intermediate') })}
    ${step.rows.length > rows.length ? html`<p class="muted">${t('process.firstRows', { n: rows.length, total: step.rows.length })}</p>` : ''}</div>`;
}

function stepCard(result, step, intermediateSet) {
  const intermediate = intermediateSet.has(step.index);
  return html`<div class="step-card">
    <div class="step-head"><span>${t('process.step', { n: step.index + 1 })} · ${sourceLabel(step.database)}</span>${stepChips(step, intermediate)}</div>
    ${step.finalSql ? sqlBlock(step.finalSql, step.index, 'final') : html`<p class="muted">${t('process.noSql')}</p>`}
    ${step.repairs.length && step.sql && step.sql !== step.finalSql ? sqlBlock(step.sql, step.index, 'original') : ''}
    ${repairList(step)}
    ${step.error ? html`<p><span class="muted">${t('process.error')}</span> <span lang="zh">${step.error}</span></p>` : ''}
    ${findingList(step)}
    ${intermediate ? intermediateRows(step) : ''}
    ${step.asOf ? html`<p class="muted">${t('process.readAt', { time: formatTime(step.asOf), source: step.asOfSource || 'unknown' })}</p>` : ''}
  </div>`;
}

function overview(turn) {
  const result = turn.result;
  const queried = [...new Set(result.steps.map((step) => step.database))];
  const item = (label, value) => html`<dt>${label}</dt><dd>${value}</dd>`;
  return html`<dl class="kv">
    ${item(t('process.traceId'), html`<span class="mono">${result.traceId || '—'}</span>
      ${result.traceId ? html`<button type="button" class="link-btn" data-action="copy-trace">${t('action.copy')}</button>` : ''}`)}
    ${item(t('process.transport'), transportLabel(result.transport))}
    ${item(t('process.route'), html`${routeLabel(result.route)}${result.confidence != null
      ? ` · ${t('process.confidence', { value: result.confidence.toFixed(2) })}` : ''}`)}
    ${result.reason ? item(t('process.reason'), html`<span lang="zh">${result.reason}</span>`) : ''}
    ${item(t('process.candidates'), result.candidateDatabases.length ? result.candidateDatabases.map(sourceLabel).join(', ') : '—')}
    ${result.candidateTables.length ? item(t('process.candidateTables'), html`<span class="mono">${result.candidateTables.join(', ')}</span>`) : ''}
    ${item(t('process.queried'), queried.length ? queried.map(sourceLabel).join(', ') : t('process.noneQueried'))}
    ${item(t('process.status'), html`<span class="mono">${result.executionStatus}</span>${result.statusSource !== 'contract'
      ? ` · ${t('process.statusDerived')}` : ''}`)}
    ${result.degraded ? item(t('process.degraded'), html`<span class="mono">${result.degradeReason || 'true'}</span>`) : ''}
    ${item(t('process.time'), html`${result.clientMs != null ? t('process.clientTime', { time: formatDuration(result.clientMs) }) : '—'}${result.serverMs != null
      ? ` · ${t('process.serverTime', { time: formatDuration(result.serverMs) })}` : ''}`)}
    ${result.totalTokens != null ? item(t('process.tokens'), String(result.totalTokens)) : ''}
  </dl>`;
}

function rawResponse(turn) {
  if (!turn.ui.open.raw) {
    return html`<details class="disclosure" data-detail="raw"><summary>${t('process.raw')}</summary></details>`;
  }
  let text = '';
  try { text = JSON.stringify(turn.result.raw, null, 2); } catch (_) { text = ''; }
  const clipped = text.length > RAW_LIMIT;
  return html`<details class="disclosure" data-detail="raw" open><summary>${t('process.raw')}</summary>
    <div class="disclosure-body"><pre class="code">${clipped ? text.slice(0, RAW_LIMIT) : text}</pre>
    ${clipped ? html`<p class="muted">${t('process.rawClipped', { n: RAW_LIMIT })}</p>` : ''}</div></details>`;
}

export function renderProcess(turn) {
  const result = turn.result;
  if (!result) return '';
  const { intermediate } = splitSteps(result, turn.question);
  const intermediateSet = new Set(intermediate.map((step) => step.index));
  return html`<details class="disclosure is-quiet" data-detail="process" ${turn.ui.open.process ? 'open' : ''}>
    <summary>${t('process.title')}</summary>
    <div class="disclosure-body">
      ${overview(turn)}
      ${result.steps.map((step) => stepCard(result, step, intermediateSet))}
      ${rawResponse(turn)}
    </div>
  </details>`;
}

/* ─── 学习模式 ─────────────────────────────────────────────────── */

function stageTimeline(stages) {
  if (!stages.length) return '';
  return html`<div><p class="learn-title">${t('learn.timeline.title')}</p>
    <p class="muted">${t('learn.timeline.note')}</p>
    <div class="timeline">${stages.map((stage) => html`
      <span class="t">${offset(stage)}</span>
      <span class="mono">${stage.stage}</span>
      <span>${has('stage.' + stage.stage) ? t('stage.' + stage.stage) : ''}${stage.step != null ? ` · ${t('process.step', { n: stage.step })}` : ''}${stage.database
        ? ` · ${sourceLabel(stage.database)}` : ''}${stage.shortPath === true ? ` · ${t('learn.shortPath')}` : ''}${stage.withMemory === true
        ? ` · ${t('learn.withMemory')}` : ''}</span>`)}</div></div>`;
}

/* 这一步在阶段事件里出现过没有。没走流式时返回 null：不知道，不是没发生。 */
function observed(stages, stepId, streamed) {
  if (!streamed) return null;
  return stages.filter((stage) => STAGE_TO_STEP[stage.stage] === stepId);
}

function observedLine(events) {
  if (events === null) return html`<li class="muted">${t('learn.notReported')}</li>`;
  if (!events.length) return html`<li class="muted">${t('learn.notObserved')}</li>`;
  const names = [...new Set(events.map((stage) => stage.stage))].join(', ');
  return html`<li>${t('learn.observed', { stages: names, at: offset(events[0]) || '—', n: events.length })}</li>`;
}

/* 一步的四种状态，只有第一种有事实可列：

   happened   响应里有这一步的结果，或者（走流式时）收到过它的阶段事件
   skipped    确实没有发生——这条消息走的是对话回答，后面的步骤根本不会跑
   unreached  请求是去查库的，但在走到这一步之前就结束了（收到的阶段事件里没有它）
   unknown    请求是去查库的、没有结果，而这条请求路径不回报阶段：不知道走到了哪

   后三种不能混成一句「没有发生」：模型服务中途断掉时，规划明明已经开始了。 */
const STATE_TEXT = { skipped: 'learn.skipped', unreached: 'learn.unreached', unknown: 'learn.unknownStage' };

function learnStep(number, id, facts, state = 'happened') {
  const happened = state === 'happened';
  return html`<li class="learn-step ${happened ? '' : 'is-skipped'}">
    <span class="learn-num" aria-hidden="true">${number}</span>
    <div>
      <p class="learn-title">${t(`learn.${id}.title`)}</p>
      <div class="happened"><p class="muted">${t(happened ? 'learn.happened' : STATE_TEXT[state])}</p>
        ${happened ? html`<ul class="bullets">${facts}</ul>` : ''}</div>
      <p class="learn-note">${t(`learn.${id}.note`)}</p>
      <p class="learn-src">${SOURCES[id]}</p>
    </div>
  </li>`;
}

function learnResult(turn) {
  const result = turn.result;
  const view = classify(result);
  const streamed = result.transport === 'a2a-stream';
  const stages = result.stages || [];
  const { intermediate } = splitSteps(result, turn.question);
  const intermediateSet = new Set(intermediate.map((step) => step.index));
  const ran = result.queriedDatabase && result.steps.length > 0;
  const queried = [...new Set(result.steps.map((step) => step.database))];
  const li = (content) => html`<li>${content}</li>`;
  const stateOf = (id, hasFacts) => {
    if (hasFacts) return 'happened';
    if (result.route !== 'database_query') return 'skipped';
    const events = observed(stages, id, streamed);
    if (events === null) return 'unknown';
    return events.length ? 'happened' : 'unreached';
  };
  // 阶段开始过、但没有结果回来（请求在这一步里结束了）：只有阶段事件这一条事实
  const noResult = li(html`<span class="muted">${t('learn.noStepResult')}</span>`);

  const understand = [
    observedLine(observed(stages, 'understand', streamed)),
    li(html`${t('learn.understand.route')} <strong>${routeLabel(result.route)}</strong>${result.confidence != null
      ? ` · ${t('process.confidence', { value: result.confidence.toFixed(2) })}` : ''}`),
    result.reason
      ? li(html`${t('learn.understand.reason')} <span lang="zh">“${result.reason}”</span>`)
      : streamed ? li(html`<span class="muted">${t('learn.understand.noReason')}</span>`) : '',
    !result.queriedDatabase ? li(t('learn.understand.noQuery')) : '',
    result.degraded ? li(html`${t('learn.degraded')} <span class="mono">${result.degradeReason}</span>`) : '',
  ];

  const retrieve = [
    observedLine(observed(stages, 'retrieve', streamed)),
    li(html`${t('learn.retrieve.candidates')} ${result.candidateDatabases.length
      ? result.candidateDatabases.map(sourceLabel).join(', ') : '—'}`),
    result.candidateTables.length
      ? li(html`${t('learn.retrieve.tables')} <span class="mono">${result.candidateTables.join(', ')}</span>`) : '',
    li(html`${t('learn.retrieve.queried')} ${queried.length ? queried.map(sourceLabel).join(', ') : '—'}`),
    result.candidateDatabases.some((database) => !queried.includes(database))
      ? li(html`<span class="muted">${t('learn.retrieve.differ')}</span>`) : '',
  ];

  const planEvents = observed(stages, 'plan', streamed);
  const shortPath = planEvents && planEvents.find((stage) => stage.shortPath !== null);
  // 这里写的是观察到的事实（上一步某一列的取值，全部出现在下一步的 SQL 里），不是服务端
  // 的声明——响应里没有「哪一步把什么交给了哪一步」这样的字段。只对上一个值时特别注明。
  const handoffs = result.steps.flatMap((step) => passedOn(result, step, turn.question).map((carried) => li(t(
    carried.values.length === 1 ? 'learn.plan.handoffOne' : 'learn.plan.handoff',
    {
      from: step.index + 1,
      to: carried.intoStep + 1,
      column: carried.column,
      values: carried.values.slice(0, 4).join(', '),
      n: carried.values.length,
    },
  ))));
  const plan = [
    observedLine(planEvents),
    shortPath ? li(t(shortPath.shortPath ? 'learn.plan.shortPath' : 'learn.plan.fullPlan')) : '',
    ran ? li(t(result.steps.length === 1 ? 'learn.plan.single' : 'learn.plan.multi', {
      n: result.steps.length,
      databases: result.steps.map((step) => `${step.index + 1}: ${sourceLabel(step.database)}`).join(' → '),
    })) : noResult,
    ...handoffs,
    result.steps.length > 1 && !handoffs.length ? li(t('learn.plan.independent')) : '',
  ];

  const generate = [
    observedLine(observed(stages, 'generate', streamed)),
    ran ? '' : noResult,
    ...result.steps.map((step) => html`<li>${t('process.step', { n: step.index + 1 })} · ${sourceLabel(step.database)}
      ${step.finalSql ? sqlBlock(step.finalSql, step.index, 'final') : html`<span class="muted">${t('process.noSql')}</span>`}
      ${step.repairs.length && step.sql && step.sql !== step.finalSql ? sqlBlock(step.sql, step.index, 'original') : ''}</li>`),
  ];

  const execute = [
    observedLine(observed(stages, 'execute', streamed)),
    ran ? '' : noResult,
    ...result.steps.map((step) => html`<li>${t('process.step', { n: step.index + 1 })}: ${stepChips(step, intermediateSet.has(step.index))}
      ${step.asOf ? html`<span class="muted">${t('process.readAt', { time: formatTime(step.asOf), source: step.asOfSource || 'unknown' })}</span>` : ''}
      ${step.error ? html`<p><span class="muted">${t('process.error')}</span> <span lang="zh">${step.error}</span></p>` : ''}
      ${repairList(step)}
      ${intermediateSet.has(step.index) ? intermediateRows(step) : ''}</li>`),
  ];

  const allFindings = view.findings;
  const units = result.steps.flatMap((step) => Object.entries(step.units).map(([column, unit]) => `${column}: ${unit}`));
  const check = [
    observedLine(observed(stages, 'check', streamed)),
    ran ? '' : noResult,
    li(html`${t('learn.check.status')} <span class="mono">execution_status=${result.executionStatus}</span>
      <span class="mono">has_risk=${String(result.hasRisk)}</span> <span class="mono">degraded=${String(result.degraded)}</span>
      <span class="mono">result_truncated=${String(result.truncated)}</span>`),
    allFindings.length
      ? html`<li>${t('learn.check.findings', { n: allFindings.length })}${result.steps.map((step) => findingList(step))}</li>`
      : li(t('learn.check.noFindings')),
    li(units.length
      ? html`${t('learn.check.units')} <span class="mono">${units.join(', ')}</span>${result.currency ? ` · currency=${result.currency}` : ''}`
      : html`<span class="muted">${t('learn.check.noUnits')}</span>`),
    result.asOf ? li(t('learn.check.asOf', { time: result.asOf, source: result.asOfSource || 'unknown' })) : '',
  ];

  return html`
    ${stageTimeline(stages)}
    ${streamed ? '' : html`<p class="muted">${t('learn.queryTransportNote')}</p>`}
    <ol>
      ${learnStep(1, 'understand', understand)}
      ${learnStep(2, 'retrieve', retrieve, stateOf('retrieve', ran || result.candidateDatabases.length > 0))}
      ${learnStep(3, 'plan', plan, stateOf('plan', ran))}
      ${learnStep(4, 'generate', generate, stateOf('generate', ran))}
      ${learnStep(5, 'execute', execute, stateOf('execute', ran))}
      ${learnStep(6, 'check', check, stateOf('check', ran))}
    </ol>
    <dl class="kv">
      <dt>${t('process.traceId')}</dt><dd><span class="mono">${result.traceId || '—'}</span>
        ${result.traceId ? html`<button type="button" class="link-btn" data-action="copy-trace">${t('action.copy')}</button>` : ''}</dd>
      <dt>${t('process.time')}</dt><dd>${result.clientMs != null ? t('process.clientTime', { time: formatDuration(result.clientMs) }) : '—'}${result.serverMs != null
        ? ` · ${t('process.serverTime', { time: formatDuration(result.serverMs) })}` : ''}${result.totalTokens != null
        ? ` · ${t('process.tokens')}: ${result.totalTokens}` : ''}</dd>
    </dl>
    ${rawResponse(turn)}`;
}

export function renderLearning(turn) {
  const transport = turn.result ? turn.result.transport : turn.transport;
  const stages = turn.result ? [] : (turn.stages || []);
  let body;
  if (turn.result) {
    body = learnResult(turn);
  } else if (turn.status === 'error') {
    body = html`${stageTimeline(turn.stages || [])}
      <p>${t('learn.failedBeforeResult')}</p>`;
  } else {
    body = stages.length
      ? stageTimeline(stages)
      : html`<p class="muted">${t(transport === 'a2a-stream' ? 'learn.waitingStream' : 'learn.waitingQuery')}</p>`;
  }
  return html`<section class="learn" aria-label="${t('learn.title')}">
    <div class="learn-head"><span class="row">${icon('learn')}${t('learn.title')}</span>
      <span class="chip chip-info">${transportLabel(transport)}</span></div>
    <div class="learn-body stack">${body}</div>
  </section>`;
}
