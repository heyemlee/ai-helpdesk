/* 把服务端的响应归一成页面用的结果对象，并判定「这条结果该显示成什么状态」。

   判定完全按契约 §4.6 来，页面不自己发明口径：
   - 业务终态读 execution_status；服务端没给才从 steps[] 推（并记下来源）；
   - 三个信任布尔必须是真正的 JSON 布尔，缺失或类型不对一律按**降低信任**的方向取值——
     has_risk 缺失当作有风险，queried_database 缺失当作没查库。
     一个「降信任的信号」缺席时若按「没有这个信号」处理，等于默认信任。 */

const DATABASE_QUERY = 'database_query';

const strictBool = (value, missing) => (typeof value === 'boolean' ? value : missing);

function deriveStatus(route, steps) {
  if (route !== DATABASE_QUERY) return 'not_applicable';
  if (!steps.some((step) => step.success)) return 'failed';
  if (!steps.every((step) => step.success)) return 'partial_success';
  return steps.reduce((sum, step) => sum + step.rowCount, 0) > 0 ? 'success' : 'no_data';
}

function normalizeStep(raw, index) {
  const rows = Array.isArray(raw.rows) ? raw.rows.filter((row) => row && typeof row === 'object') : [];
  const columns = [];
  rows.forEach((row) => {
    Object.keys(row).forEach((key) => { if (!columns.includes(key)) columns.push(key); });
  });
  return {
    index,
    database: String(raw.database || ''),
    sql: String(raw.sql || ''),
    finalSql: String(raw.final_sql || raw.sql || ''),
    success: strictBool(raw.success, false),
    rowCount: Number.isInteger(raw.row_count) ? raw.row_count : rows.length,
    truncated: strictBool(raw.truncated, false),
    rowLimit: Number.isInteger(raw.row_limit) ? raw.row_limit : null,
    error: raw.error ? String(raw.error) : '',
    repairs: Array.isArray(raw.repair_attempts) ? raw.repair_attempts : [],
    findings: Array.isArray(raw.risk_findings) ? raw.risk_findings : [],
    asOf: raw.as_of || null,
    asOfSource: raw.as_of_source || null,
    units: raw.column_units && typeof raw.column_units === 'object' ? raw.column_units : {},
    sourceRecords: Array.isArray(raw.source_records) ? raw.source_records : [],
    columns,
    rows,
  };
}

/* payload：/query 的响应体，或 A2A artifact 的 data part 加上 answer。
   契约 §4.4.1：逐步骤日志的契约面名字是 steps，step_logs 是过渡期的旧名字，两个都认。 */
export function normalizeResult(payload, { transport, stages = [], clientMs = null } = {}) {
  const rawSteps = Array.isArray(payload.steps)
    ? payload.steps
    : Array.isArray(payload.step_logs) ? payload.step_logs : null;
  const steps = (rawSteps || []).map(normalizeStep);
  const route = String(payload.route || '');
  const declared = typeof payload.execution_status === 'string' ? payload.execution_status : '';

  return {
    traceId: String(payload.trace_id || ''),
    transport,
    receivedAt: new Date().toISOString(),
    route,
    confidence: typeof payload.confidence === 'number' ? payload.confidence : null,
    reason: String(payload.reason || ''),
    answer: String(payload.answer || ''),
    queriedDatabase: strictBool(payload.queried_database, false),
    candidateDatabases: Array.isArray(payload.selected_databases) ? payload.selected_databases : [],
    candidateTables: [],            // /query 不带；查完从 /history 回填
    executionStatus: declared || (rawSteps ? deriveStatus(route, steps) : 'failed'),
    statusSource: declared ? 'contract' : rawSteps ? 'derived' : 'missing',
    hasRisk: strictBool(payload.has_risk, true),
    degraded: strictBool(payload.degraded, true),
    degradeReason: String(payload.degrade_reason || ''),
    truncated: typeof payload.result_truncated === 'boolean'
      ? payload.result_truncated
      : steps.some((step) => step.truncated),
    asOf: payload.as_of || null,
    asOfSource: payload.as_of_source || null,
    currency: typeof payload.currency === 'string' && /^[A-Z]{3}$/.test(payload.currency)
      ? payload.currency
      : null,
    steps,
    stages,
    clientMs,
    serverMs: null,                 // 服务端记账的耗时与 token，查完从 /history 回填
    totalTokens: null,
    raw: payload,
  };
}

/* 给人看的状态。kind 互斥，flags 可以叠加。

   conversation  没查库：这段话是模型说的，不是数据
   failed        查库路径，但一步都没成功
   partial       有的步骤成功、有的失败
   no_data       查到了，答案是「没有」——不是错误
   success       有数据 */
export function classify(result) {
  const status = result.executionStatus;
  let kind = 'failed';
  if (!result.queriedDatabase || status === 'not_applicable') kind = 'conversation';
  else if (status === 'success') kind = 'success';
  else if (status === 'no_data') kind = 'no_data';
  else if (status === 'partial_success') kind = 'partial';

  const findings = result.steps.flatMap((step) =>
    step.findings.map((finding) => ({ ...finding, stepIndex: step.index, database: step.database })));

  // 空结果本身就会被校验器记一条 empty_result。只有这一类发现时，「没有匹配的记录」
  // 已经把话说完了，不再额外挂一条「需要核实」——那样两个状态会糊成一个。
  const onlyEmptyResult = findings.length > 0
    && findings.every((finding) => finding.anomaly === 'empty_result');
  const needsReview = result.hasRisk && !(kind === 'no_data' && onlyEmptyResult);

  return {
    kind,
    needsReview,
    truncated: result.truncated && (kind === 'success' || kind === 'partial'),
    degraded: result.degraded,
    repaired: result.steps.some((step) => step.repairs.length > 0),
    findings,
    failedSteps: result.steps.filter((step) => !step.success),
  };
}

/* 哪一步是「答案」，哪些只是中间步骤。

   跨库查询把上一步的结果写成字面量拼进下一步的 SQL。响应里没有一个字段说
   「这一步是中间步骤」，页面只能看证据（见 passedOn）。判定从严：

   只有**每一列**都被带进了后面 SQL 的步骤才算中间步骤——它交出去的就是它的全部内容，
   收进折叠里什么也没丢。还带着别的列（名称、金额）的步骤，那些列是答案的一部分；
   把它当中间步骤收起来，结论里就少了一张表，而且没人知道少了。
   判不准的时候多显示一张表，不少显示。 */
export function splitSteps(result, question = '') {
  const succeeded = result.steps.filter((step) => step.success);
  if (!succeeded.length) return { answers: [], intermediate: [] };

  const last = succeeded[succeeded.length - 1];
  const answers = [];
  const intermediate = [];
  succeeded.forEach((step) => {
    const carried = step === last ? [] : passedOn(result, step, question);
    const consumed = carried.length > 0
      && step.columns.every((column) => carried.some((item) => item.column === column));
    (consumed ? intermediate : answers).push(step);
  });
  return { answers, intermediate };
}

// 服务端每列最多回填这么多个取值（pipeline/text2sql_pipeline.py 的 _MAX_BACKFILL_LITERALS）
const MAX_CARRIED_VALUES = 50;
const WORD_CHAR = /[\w.'"]/;

/* 一个值照服务端的写法变成 SQL 字面量：数字不加引号，其余加单引号并转义。 */
function literalsOf(value) {
  if (typeof value === 'boolean') return [value ? '1' : '0'];
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    // Python 把 1200.0 写成 "1200.0"，到了这边已经是 1200：两种写法都认
    return Number.isInteger(value) ? [String(value), `${value}.0`] : [String(value)];
  }
  if (typeof value === 'string' && value !== '') return ["'" + value.replace(/'/g, "''") + "'"];
  return null;
}

/* text 作为一个完整的记号出现在 sql 里的位置；没有返回 -1。 */
function tokenAt(sql, text) {
  let from = 0;
  for (;;) {
    const at = sql.indexOf(text, from);
    if (at === -1) return -1;
    const before = at > 0 ? sql[at - 1] : ' ';
    const after = at + text.length < sql.length ? sql[at + text.length] : ' ';
    const quoted = text[0] === "'";
    if (quoted || (!WORD_CHAR.test(before) && !WORD_CHAR.test(after))) return at;
    from = at + 1;
  }
}

const literalAt = (sql, forms) => forms.reduce((found, form) => (found !== -1 ? found : tokenAt(sql, form)), -1);

/* 这一步的哪些列被原样带进了后面步骤的 SQL：[{ column, values, intoStep }]。

   证据按列算：这一列返回的**所有不同取值**都作为字面量出现在后面同一步的 SQL 里。
   单个值碰巧出现太常见（两步各自按 'open' 筛选），整列都对得上才算数。另外两条收紧：

   - 问题里本来就写着的值不算证据——两步都按它筛选，两边的 SQL 里自然都有；
   - 孤零零一个数字只在它紧跟着 `=` 或 `IN (` 时才认：一个 3 到处都是。 */
export function passedOn(result, step, question = '') {
  const later = result.steps.filter((other) => other.index > step.index && other.finalSql);
  if (!later.length || !step.rows.length) return [];
  const asked = String(question || '');
  const found = [];

  step.columns.forEach((column) => {
    const seen = new Set();
    const values = [];
    for (const row of step.rows) {
      const value = row[column];
      if (value === null || value === undefined || value === '') continue;
      const key = `${typeof value}:${value}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const forms = literalsOf(value);
      if (!forms) return;                 // 写不成字面量的值（对象、数组）：这一列不可能被原样带过去
      values.push({ value, forms });
      if (values.length >= MAX_CARRIED_VALUES) break;
    }
    if (!values.length) return;

    const target = later.find((other) => values.every((item) => literalAt(other.finalSql, item.forms) !== -1));
    if (!target) return;
    if (values.every((item) => asked.includes(String(item.value)))) return;
    if (values.length === 1 && typeof values[0].value !== 'string') {
      const at = literalAt(target.finalSql, values[0].forms);
      if (!/(=|\bIN\s*\()\s*$/i.test(target.finalSql.slice(0, at))) return;
    }
    found.push({ column, values: values.map((item) => item.value), intoStep: target.index });
  });
  return found;
}

/* /history 的一条记录 → 历史页的状态。老记录没有 execution_status，
   那时只能退回看 success / error，而且不把「不知道」说成「成功」。 */
export function historyStatus(item) {
  const status = item.execution_status || '';
  const hasSql = (item.sqls || []).length > 0;
  if (item.error || item.status_code >= 400 || status === 'failed') return 'failed';
  if (item.degraded && !hasSql) return 'failed';     // 模型不可用，也没有可重放的 SQL
  if (status === 'partial_success') return 'partial';
  if (status === 'no_data') return 'no_data';
  if (item.has_risk) return 'review';
  if (item.degraded) return 'degraded';
  if (status === 'not_applicable' || (item.route && item.route !== DATABASE_QUERY)) return 'conversation';
  if (status === 'success') return 'success';
  if (item.success === false) return 'failed';
  return 'completed';                                // 老记录：只知道跑完了，不知道有没有数据
}
