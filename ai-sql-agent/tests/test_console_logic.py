"""
查询界面里几段纯逻辑的行为测试：用 Node 直接加载 `api/static/js` 下的真实模块来跑。

`test_console_frontend.py` 读文件查不变量（模块在不在、文案齐不齐），查不了「算得对不对」。
这里补的是后者，都是评审里实际跑出过错的地方：

- 跨库查询里哪一步算中间步骤（判错了，答案里会少一张表，或者多出一张不相干的）；
- 表格排序（数字以字符串到达页面时不能按字符比）；
- 数字显示（一个不是零的数不能显示成 0）；
- 路由（不认识的 hash 不该把人带去别的页面）；
- 请求层（响应体读不出来不是「成功」）与目录加载的先后次序、身份的备用来源；
- 数据浏览：一张表「正在怎么看」和地址互转、表与表怎么连、时间轴怎么并、
  取值怎么配色、概况什么时候取什么时候作废；
- 模型连接的表单：现有的 Key 什么时候可以不重填、哪种页面不适合填 Key。

没有装 Node 的环境整组跳过：页面本身不依赖 Node，这只是测试手段。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.api.app import STATIC_DIR

JS_DIR = STATIC_DIR / "js"
NODE = shutil.which("node")

HARNESS = r"""
import { readFileSync } from 'node:fs';
const JS = process.argv[2];
const load = (name) => import(`${JS}/${name}`);
const out = {};

// ---- model.js：哪一步是中间步骤 ------------------------------------------------
{
  const { normalizeResult, passedOn, splitSteps } = await load('model.js');
  const make = (steps) => normalizeResult({
    route: 'database_query', queried_database: true, has_risk: false, degraded: false,
    execution_status: 'success', steps,
  }, { transport: 'query' });
  const step = (database, sql, rows) => ({ database, sql, final_sql: sql, success: true, row_count: rows.length, rows });
  const verdict = (result, question = '') => {
    const split = splitSteps(result, question);
    return {
      answers: split.answers.map((item) => item.index + 1),
      intermediate: split.intermediate.map((item) => item.index + 1),
      carried: passedOn(result, result.steps[0], question).map((item) => [item.column, item.values.length, item.intoStep + 1]),
    };
  };
  out.steps = {
    handoff: verdict(make([
      step('pm_db', 'SELECT "project_code" FROM "project" WHERE "actual_cost" > "budget_amt";',
        [{ project_code: 'J24001' }, { project_code: 'J24003' }, { project_code: 'J24006' }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) AS total FROM purchase_order WHERE job_no IN ('J24001', 'J24003', 'J24006') GROUP BY job_no;",
        [{ job_no: 'J24001', total: 127200 }]),
    ])),
    handoffWithExtras: verdict(make([
      step('pm_db', 'SELECT project_code, project_name, actual_cost FROM project WHERE actual_cost > budget_amt',
        [{ project_code: 'J24001', project_name: 'Maple Kitchen', actual_cost: 91000 },
         { project_code: 'J24003', project_name: 'Quartz Bath', actual_cost: 45000 }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) t FROM purchase_order WHERE job_no IN ('J24001', 'J24003') GROUP BY job_no",
        [{ job_no: 'J24001', t: 1 }]),
    ])),
    sharedFilter: verdict(make([
      step('erp_db', "SELECT order_no, status FROM sales_order WHERE status = 'open'",
        [{ order_no: 'SO-1', status: 'open' }, { order_no: 'SO-2', status: 'open' }]),
      step('mes_db', "SELECT wo_no, status FROM work_order WHERE status = 'open'", [{ wo_no: 'WO-9', status: 'open' }]),
    ])),
    valueFromQuestion: verdict(make([
      step('pm_db', "SELECT project_code FROM project WHERE project_code = 'J24001'", [{ project_code: 'J24001' }]),
      step('erp_db', "SELECT po_no FROM purchase_order WHERE job_no = 'J24001'", [{ po_no: 'PO-1' }]),
    ]), 'Show purchase orders for project J24001'),
    numericKeys: verdict(make([
      step('erp_db', 'SELECT supplier_id FROM supplier WHERE on_time_rate < 0.9',
        [{ supplier_id: 3 }, { supplier_id: 15 }, { supplier_id: 27 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id IN (3, 15, 27)', [{ batch_no: 'B1' }]),
    ])),
    loneNumberElsewhere: verdict(make([
      step('erp_db', 'SELECT COUNT(*) AS n FROM supplier', [{ n: 3 }]),
      step('wms_db', 'SELECT batch_no FROM batch ORDER BY received_on DESC LIMIT 3', [{ batch_no: 'B1' }]),
    ])),
    loneNumberAsKey: verdict(make([
      step('erp_db', "SELECT supplier_id FROM supplier WHERE supplier_name = 'Canyon Crest Quartz'", [{ supplier_id: 1042 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id = 1042', [{ batch_no: 'B1' }]),
    ])),
    pythonFloat: verdict(make([
      step('a', 'SELECT amount FROM t', [{ amount: 1200 }, { amount: 88.5 }]),
      step('b', 'SELECT x FROM u WHERE amount IN (1200.0, 88.5)', [{ x: 1 }]),
    ])),
    digitsInsideOtherNumbers: verdict(make([
      step('a', 'SELECT id FROM t', [{ id: 12 }, { id: 7 }]),
      step('b', 'SELECT x FROM u WHERE id IN (123, 77) AND rate > 1.12', [{ x: 1 }]),
    ])),
  };
}

// ---- table.js / format.js ------------------------------------------------------
{
  const { sortRows } = await load('table.js');
  const { formatNumber, recordDates } = await load('format.js');
  const column = (values, direction = 'asc') => sortRows(values.map((value) => [value]), 0, direction).map((row) => row[0]);
  out.sort = {
    numericStrings: column(['-0.50', '-9.00', '-10.00', '3', '12.5']),
    numericStringsDesc: column(['-0.50', '-9.00', '-10.00', '3', '12.5'], 'desc'),
    longIntegers: column(['9007199254740993', '9007199254740992', '9007199254740994']),
    mixedTypes: column([10, '9', 2.5, '-1']),
    emptiesLast: column([null, '2', '', '1'], 'desc'),
    text: column(['banana', 'Apple', 'cherry']),
  };
  out.numbers = {
    tiny: formatNumber(0.00001234),
    tinyNegative: formatNumber(-0.00001),
    tinyMoney: formatNumber(0.004, { unit: 'USD' }),
    zero: formatNumber(0),
    zeroMoney: formatNumber(0, { unit: 'USD' }),
    money: formatNumber(31600, { unit: 'USD' }),
    ratio: formatNumber(0.88),
    year: formatNumber(2024),
    large: formatNumber(127200),
  };
  const dates = (values) => recordDates([{ key: 'c' }], values.map((value) => ({ c: value })), (row, col) => row[col.key])
    .map((range) => [range.min, range.max]);
  out.dates = {
    plain: dates(['2024-01-25', '2024-04-22']),
    withTime: dates(['2024-03-31 10:00:00', '2024-03-30T09:00:00']),
    documentNumbers: dates(['2024-10-12345', '2024-10-98765']),
    impossible: dates(['2024-13-01']),
  };

  // 数据字典里的文字：有当前语言的版本用它；没有就给原文并标出来，不替它翻译
  const { localized } = await load('format.js');
  const { state } = await load('state.js');
  const entry = {
    term: '准时率', term_en: 'On-time rate',
    definition: '按约定交期到货的批次占比。',
    definition_en: 'Share of deliveries that arrived by the promised date.',
    usage: '用于筛选供应商', note: 'Same supplier', empty_en: '',
  };
  const read = (lang) => {
    state.lang = lang;
    return Object.fromEntries(['term', 'definition', 'usage', 'note', 'empty', 'absent']
      .map((key) => [key, localized(entry, key)]));
  };
  out.localized = { en: read('en'), zh: read('zh'), nothing: localized(null, 'term') };
  state.lang = 'en';
}

// ---- main.js：路由（函数取出来单独跑，整个模块要 DOM） ---------------------------
{
  const source = readFileSync(new URL(`${JS}/main.js`), 'utf8');
  const body = source.match(/function parseRoute\(\) \{[\s\S]*?\n\}/)[0];
  const parse = (hash) => new Function('location', `${body}; return parseRoute();`)({ hash });
  out.routes = Object.fromEntries(['', '#', '#/', '#/ask', '#/data', '#/data/erp_db', '#/data/erp_db/supplier', '#/history',
    '#main', '#/nope', '#/data/%E0%A4%A', '#/data/erp_db/supplier?q=a/b&f=c%3Aeq%3A%253F', '#/data?tab=fields',
    '#/data/erp_db/odd%3Fname'].map((hash) => [hash, parse(hash)]));
}

// ---- dataRoute.js：一张表「正在怎么看」和地址互转 -------------------------------
{
  const { dataHref, formatView, freshView, parseView } = await load('dataRoute.js');
  const view = {
    ...freshView(), search: 'a b&c', page: 3, pageSize: 50, tab: 'fields',
    sort: { key: 'signed on', direction: 'desc' },
    filters: [
      { column: 'status', op: 'eq', value: '已取消' },
      { column: 'odd:name?x/y', op: 'contains', value: 'a:b&c=d #1' },
      { column: 'notes', op: 'is_empty', value: '' },
    ],
  };
  out.viewRoute = {
    roundTrip: JSON.stringify(parseView(formatView(view))) === JSON.stringify(view),
    plain: formatView(freshView()),
    href: dataHref('shop db', 'a/b?c', { filters: [{ column: 'k', op: 'eq', value: 'J-1' }] }),
    home: [dataHref(), dataHref('x_db'), dataHref('', 'ignored')],
    junk: parseView('q=x&f=broken&f=a:regexp:b&f=%E0%A4%A:eq:1&sort=nodirection&page=-2&size=7&tab=sql&f=ok:eq:1'),
    tooMany: parseView(Array.from({ length: 9 }, (_, i) => `f=c${i}:eq:1`).join('&')).filters.length,
  };
}

// ---- links.js：表与表怎么连 -----------------------------------------------------
{
  const { buildLinks, crossDatabaseLinks, relatedTables } = await load('links.js');
  const column = (name, references = [], extra = {}) => ({ name, references, ...extra });
  const ref = (relation, database, table, name) => ({ relation, database, table, column: name });
  const domains = [
    { database: 'a_db', tables: [
      { name: 'orders', columns: [
        column('order_no', [
          ref('cross_database', 'b_db', 'work', 'order_no'),
          ref('cross_database', 'c_db', 'ticket', 'ref_no'),
          ref('cross_database', 'z_db', 'hidden', 'order_no'),      // 对端不在目录里（没权限）
        ], { primary_key: true }),
        column('customer'),
      ] },
      { name: 'items', columns: [column('order_no', [ref('foreign_key', 'a_db', 'orders', 'order_no')]), column('sku')] },
    ] },
    { database: 'b_db', tables: [
      { name: 'work', columns: [column('order_no', [ref('cross_database', 'a_db', 'orders', 'order_no')])] },
    ] },
    { database: 'c_db', tables: [
      { name: 'ticket', columns: [
        column('ref_no', [ref('cross_database', 'a_db', 'orders', 'order_no')]),
        column('part', [ref('cross_database', 'c_db', 'stock', 'name')]),
      ] },
      { name: 'stock', columns: [column('name', [ref('cross_database', 'c_db', 'ticket', 'part')], { primary_key: true })] },
    ] },
  ];
  const links = buildLinks(domains);
  const name = (member) => `${member.database}.${member.table}.${member.column}`;
  const related = (database, table) => relatedTables(links, database, table).map((item) => [item.column, item.others.map(name)]);
  out.links = {
    groups: links.groups.map((group) => ({ hub: name(group.hub), members: group.members.map(name), databases: group.databases })),
    edges: links.edges.length,
    cached: buildLinks(domains) === links,
    fromTicket: related('c_db', 'ticket'),
    fromStock: related('c_db', 'stock'),
    lonely: related('a_db', 'nothing'),
    across: crossDatabaseLinks(links, 'a_db').map((group) => [group.domain.database, group.links.map((link) => `${name(link.mine)}>${name(link.other)}`)]),
    empty: buildLinks(null).groups.length,
  };
}

// ---- charts.js / format.js：时间轴怎么并、取值怎么配色、数量怎么写 ----------------
{
  const { rebucket, seriesSlots } = await load('charts.js');
  const { formatCompact, formatCount, formatShare } = await load('format.js');
  const { state } = await load('state.js');
  state.lang = 'en';
  const months = (from, count) => Array.from({ length: count }, (_, offset) => {
    const index = from + offset;
    return { period: `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}`, count: 1 };
  });
  const shape = (result) => ({ grain: result.grain, bars: result.bars.length, first: result.bars[0], last: result.bars[result.bars.length - 1] });
  out.buckets = {
    gaps: rebucket([{ period: '2025-11', count: 3 }, { period: '2026-02', count: 5 }, { period: 'junk', count: 9 }]),
    quarters: shape(rebucket(months(2020 * 12 + 1, 40))),       // 2020-02 起 40 个月
    years: shape(rebucket(months(2000 * 12, 200))),             // 2000-01 起 200 个月
    none: rebucket([]),
  };
  const facet = (values, extra = {}) => ({
    complete: true, ordered_by: 'count', values: values.map((value, index) => ({ value, count: 10 - index })), ...extra,
  });
  const slots = (item) => [...seriesSlots(item)];
  out.slots = {
    declared: slots(facet(['open', 'waiting', 'closed'], { ordered_by: 'declared' })),
    byValue: slots(facet(['zeta', 'alpha', 10, 9])),
    tooMany: slots(facet(['a', 'b', 'c', 'd', 'e', 'f', 'g'])),
    incomplete: slots(facet(['a', 'b'], { complete: false })),
    nothing: slots(null),
  };
  out.counts = {
    count: formatCount(1200),
    compact: [formatCompact(1200), formatCompact(47029260.75), formatCompact(2356.65, { unit: 'USD' }), formatCompact(39.5)],
    share: [formatShare(1, 1200), formatShare(550, 1200), formatShare(60, 1200), formatShare(0, 10), formatShare(1, 0)],
  };
}

// ---- api.js / catalog.js：假的 fetch -------------------------------------------
{
  const calls = [];
  let handler = null;
  globalThis.fetch = (url, options) => { calls.push(String(url)); return handler(String(url), options); };
  const json = (status, body) => Promise.resolve({ ok: status < 400, status, json: async () => body, headers: { get: () => 'application/json' } });
  const { api } = await load('api.js');
  const { catalog, ensureIdentity, loadCatalog } = await load('catalog.js');
  const { state } = await load('state.js');
  const caught = async (promise) => { try { await promise; return null; } catch (error) { return { name: error.name, status: error.status, code: error.code }; } };

  handler = () => Promise.resolve({ ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); } });
  out.nonJsonSuccess = await caught(api.health());
  handler = () => Promise.resolve({ ok: true, status: 200, json: async () => { const error = new Error('aborted'); error.name = 'AbortError'; throw error; } });
  out.abortedBody = await caught(api.health());

  // 先发的目录请求后回来：不能盖掉后发的那一份
  const gates = [];
  handler = (url) => new Promise((resolve) => {
    if (url.includes('/catalog')) gates.push(resolve);
    else resolve(json(404, {}));
  });
  const first = loadCatalog();
  const second = loadCatalog({ refresh: true });
  const page = (user) => json(200, { domains: [], identity: { user_id: user }, service: { streaming: false } });
  gates[1](await page('new-identity'));
  await second;
  gates[0](await page('old-identity'));
  await first;
  out.sequencing = { identity: state.identity && state.identity.user_id, status: catalog.status };

  // 数据浏览关掉了：身份从 /whoami 来，提问不受连累
  state.identity = null;
  state.service = null;
  calls.length = 0;
  handler = (url) => (url.includes('/catalog')
    ? json(404, { detail: { code: 'disabled', message: 'off' } })
    : json(200, { identity: { user_id: 'buyer' }, service: { streaming: true } }));
  await loadCatalog({ refresh: true });
  out.catalogOff = {
    status: catalog.status, identity: state.identity && state.identity.user_id,
    streaming: state.service && state.service.streaming, calls: calls.map((url) => url.split('?')[0]),
  };

  // 目录出错、当时连身份也没问到；发问前再问一次
  state.identity = null;
  let whoami = 0;
  handler = (url) => {
    if (url.includes('/catalog')) return json(500, { detail: 'boom' });
    whoami += 1;
    return whoami === 1 ? json(503, { detail: 'later' }) : json(200, { identity: { user_id: 'late' }, service: {} });
  };
  await loadCatalog({ refresh: true });
  const before = state.identity;
  const ensured = await ensureIdentity();
  out.lateIdentity = { before, ensured, identity: state.identity && state.identity.user_id, status: catalog.status };

  // ---- profile.js：概况什么时候取、什么时候作废 ----
  const { findProfile, knownRowCount, profileSupport, requestProfile } = await load('profile.js');
  const profileCalls = () => calls.filter((url) => url.includes('/profile')).length;
  const settle = async () => { for (let turn = 0; turn < 3; turn += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };
  const serve = (service) => (url) => (url.includes('/profile')
    ? json(200, { total_rows: 7, columns: [], facets: [], timelines: [] })
    : json(200, { domains: [], identity: { user_id: 'u' }, service }));

  // 没有概况接口的旧版本进程：一条请求都不发
  handler = serve({ streaming: false });
  await loadCatalog({ refresh: true });
  calls.length = 0;
  const unsupported = { support: profileSupport(), entry: requestProfile('a_db', 't'), calls: profileCalls() };
  // 管理员关掉了
  handler = serve({ profile: { enabled: false } });
  await loadCatalog({ refresh: true });
  const off = { support: profileSupport(), entry: requestProfile('a_db', 't') };

  handler = serve({ profile: { enabled: true } });
  await loadCatalog({ refresh: true });
  calls.length = 0;
  const asked = requestProfile('a_db', 't');
  const askedAgain = requestProfile('a_db', 't');
  const countOnly = requestProfile('a_db', 't', { breakdowns: false });
  await settle();
  const loaded = {
    same: asked === askedAgain && countOnly === asked, status: asked.status, calls: profileCalls(),
    count: knownRowCount('a_db', 't'), neverAsked: findProfile('a_db', 'other'),
  };
  requestProfile('a_db', 't', { filters: [{ column: 'c', op: 'eq', value: 'x' }] });
  requestProfile('a_db', 't', { filters: [{ column: 'c', op: 'eq', value: 'x' }] });
  await settle();
  const filteredCalls = profileCalls();
  // 目录刷新（换了身份、点了刷新）：上一份概况作废
  await loadCatalog({ refresh: true });
  out.profiles = {
    unsupported, off, loaded, filteredCalls,
    afterReload: { count: knownRowCount('a_db', 't'), found: findProfile('a_db', 't') },
  };
}

// ---- modelForm.js：模型连接表单里的几个判断 ----------------------------------
{
  const {
    canKeepKey, insecurePage, knownRejection, modelChoices, originOf, prefill, requestBody,
  } = await load('modelForm.js');
  const status = {
    provider: 'anthropic',
    providers: {
      anthropic: {
        base_url: 'https://relay.example.com', official_base_url: 'https://api.anthropic.com',
        chat_model: 'claude-sonnet-5', coder_model: 'claude-opus-5-5',
        key_configured: true, key_address: 'https://relay.example.com',
      },
      openai: {
        base_url: 'https://api.openai.com/v1', official_base_url: 'https://api.openai.com/v1',
        chat_model: 'gpt-5.6-luna', coder_model: 'gpt-5.6-luna',
        key_configured: false, key_address: 'https://api.openai.com',
      },
    },
  };
  const keep = (provider, baseUrl) => canKeepKey(status, { provider, baseUrl });
  out.modelForm = {
    origins: [
      'https://Relay.Example.com:443/v1/messages', 'http://localhost:11434/v1', 'http://[::1]:9000',
      'relay.example.com', 'ftp://relay.example.com', '',
    ].map(originOf),
    prefill: [prefill(status, 'anthropic'), prefill(status, 'openai'), prefill(null, 'openai')],
    keep: {
      sameHost: keep('anthropic', 'https://relay.example.com/v1'),
      samePadded: keep('anthropic', '  https://RELAY.example.com  '),
      otherHost: keep('anthropic', 'https://other.example.com'),
      lookalike: keep('anthropic', 'https://relay.example.com.evil.example'),
      otherScheme: keep('anthropic', 'http://relay.example.com'),
      otherPort: keep('anthropic', 'https://relay.example.com:8443'),
      blankMeansOfficial: keep('anthropic', ''),
      notAnAddress: keep('anthropic', 'relay'),
      noKeyYet: keep('openai', 'https://api.openai.com/v1'),
      unknownStatus: canKeepKey(null, { provider: 'anthropic', baseUrl: 'https://relay.example.com' }),
    },
    insecure: [
      { protocol: 'http:', hostname: 'localhost' }, { protocol: 'http:', hostname: '127.0.0.1' },
      { protocol: 'http:', hostname: '[::1]' }, { protocol: 'http:', hostname: 'console.localhost' },
      { protocol: 'https:', hostname: 'sql.example.com' },
      { protocol: 'http:', hostname: 'sql.example.com' }, { protocol: 'http:', hostname: '192.168.1.20' },
      { protocol: 'http:', hostname: '127.evil.example' }, { protocol: 'http:', hostname: 'localhost.evil.example' },
    ].map(insecurePage),
    body: requestBody(
      { provider: 'openai', baseUrl: ' https://relay.example.com/v1 ', chatModel: ' some-model ', coderModel: '' },
      '  sk-typed  ',
    ),
    choices: [
      modelChoices(['gpt-6-luna', 'GPT-5.6-luna', ''], 'gpt-6-luna', [' claude-x ', null], []),
      modelChoices([], ''),
    ],
    rejection: [
      knownRejection({ access_faults: [
        { permanent: false, scope: 'model' }, { permanent: true, scope: 'credential', status_code: 401 },
      ] }),
      knownRejection({ access_faults: [{ permanent: true, scope: 'model' }, { permanent: false, scope: 'credential' }] }),
      knownRejection({}),
    ],
  };
}

// ---- modelForm.js：Embedding 表单 -------------------------------------------------
{
  const { canKeepEmbeddingKey, embeddingPrefill, embeddingRequestBody } = await load('modelForm.js');
  const defaults = { base_url: 'https://api.openai.com/v1', model: 'text-embedding-3-small', dimensions: 1024 };
  const local = { embedding: { backend: 'local', model: 'local_hash', dimensions: 1024, defaults,
    reusable_key_addresses: ['https://api.openai.com'] } };
  const remote = { embedding: { backend: 'remote', base_url: 'https://relay.example.com/v1',
    model: 'text-embedding-3-large', dimensions: 3072, defaults, reusable_key_addresses: ['https://relay.example.com'] } };
  const keep = (status, baseUrl) => canKeepEmbeddingKey(status, { baseUrl });
  out.embeddingForm = {
    prefill: [embeddingPrefill(local), embeddingPrefill(remote), embeddingPrefill(null)],
    keep: {
      chatHostOfficial: keep(local, 'https://api.openai.com/v1'),
      blankMeansOfficial: keep(local, ''),
      otherHost: keep(local, 'https://relay.example.com/v1'),
      currentHost: keep(remote, 'https://relay.example.com'),
      lookalike: keep(remote, 'https://relay.example.com.evil.example/v1'),
      noStatus: canKeepEmbeddingKey(null, { baseUrl: 'https://api.openai.com/v1' }),
    },
    body: [
      embeddingRequestBody({ baseUrl: ' https://api.openai.com/v1 ', model: ' text-embedding-3-small ', dimensions: ' 1536 ' }, ' sk-x '),
      embeddingRequestBody({ baseUrl: '', model: '', dimensions: 'abc' }, ''),
      embeddingRequestBody({ baseUrl: '', model: '', dimensions: '12.5' }, ''),
    ],
  };
}

console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "没有安装 Node：跳过页面逻辑的行为测试")
class ConsoleLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "harness.mjs"
            script.write_text(HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(script), JS_DIR.as_uri()],
                capture_output=True, text=True, timeout=60,
            )
        if completed.returncode != 0:
            raise AssertionError(f"页面脚本在 Node 里跑不起来：\n{completed.stderr}")
        cls.out = json.loads(completed.stdout.strip().splitlines()[-1])

    # ---- 中间步骤 ----

    def test_a_step_whose_keys_all_went_into_the_next_sql_is_intermediate(self):
        steps = self.out["steps"]
        self.assertEqual(steps["handoff"]["intermediate"], [1])
        self.assertEqual(steps["handoff"]["answers"], [2])
        self.assertEqual(steps["handoff"]["carried"], [["project_code", 3, 2]])

    def test_numeric_keys_are_recognised_without_quotes(self):
        steps = self.out["steps"]
        # 服务端把数字写成不带引号的字面量；Python 的 1200.0 到了页面是 1200
        self.assertEqual(steps["numericKeys"]["intermediate"], [1])
        self.assertEqual(steps["loneNumberAsKey"]["intermediate"], [1])
        self.assertEqual(steps["pythonFloat"]["intermediate"], [1])

    def test_a_step_that_also_carries_other_columns_stays_in_the_answer(self):
        # 项目名称和成本没有被带进下一步：它们是答案的一部分，收进折叠里就丢了
        verdict = self.out["steps"]["handoffWithExtras"]
        self.assertEqual((verdict["answers"], verdict["intermediate"]), ([1, 2], []))

    def test_coincidences_are_not_handoffs(self):
        steps = self.out["steps"]
        for name in ("sharedFilter", "valueFromQuestion", "loneNumberElsewhere", "digitsInsideOtherNumbers"):
            self.assertEqual(steps[name]["intermediate"], [], name)
            self.assertEqual(steps[name]["answers"], [1, 2], name)
        # 问题里写着的值、LIMIT 后面的数字、别的数字里的一段，都不算「带过去了」
        for name in ("valueFromQuestion", "loneNumberElsewhere", "digitsInsideOtherNumbers"):
            self.assertEqual(steps[name]["carried"], [], name)

    # ---- 排序与显示 ----

    def test_numbers_sort_as_numbers_even_when_they_arrive_as_text(self):
        sort = self.out["sort"]
        self.assertEqual(sort["numericStrings"], ["-10.00", "-9.00", "-0.50", "3", "12.5"])
        self.assertEqual(sort["numericStringsDesc"], ["12.5", "3", "-0.50", "-9.00", "-10.00"])
        self.assertEqual(sort["mixedTypes"], ["-1", 2.5, "9", 10])
        # 超过 2^53 的整数：按 Number 比三个都相等
        self.assertEqual(
            sort["longIntegers"], ["9007199254740992", "9007199254740993", "9007199254740994"]
        )
        self.assertEqual(sort["emptiesLast"], ["2", "1", None, ""])
        self.assertEqual(sort["text"], ["Apple", "banana", "cherry"])

    def test_a_non_zero_number_is_never_shown_as_zero(self):
        numbers = self.out["numbers"]
        self.assertEqual(numbers["tiny"], "0.00001234")
        self.assertEqual(numbers["tinyNegative"], "-0.00001")
        self.assertEqual(numbers["tinyMoney"], "0.004")
        # 平常的写法不变
        self.assertEqual(numbers["zero"], "0")
        self.assertEqual(numbers["zeroMoney"], "0.00")
        self.assertEqual(numbers["money"], "31,600.00")
        self.assertEqual(numbers["ratio"], "0.88")
        self.assertEqual(numbers["year"], "2024")
        self.assertEqual(numbers["large"], "127,200")

    def test_dictionary_text_uses_the_readers_language_and_never_translates(self):
        english, chinese = self.out["localized"]["en"], self.out["localized"]["zh"]

        # 写了英文版：英文界面用它，不标「原文」
        self.assertEqual(english["term"], {"text": "On-time rate", "original": False})
        self.assertEqual(
            english["definition"]["text"], "Share of deliveries that arrived by the promised date."
        )
        # 没写英文版：照原文给，并标明这是原文——页面不替数据字典翻译口径
        self.assertEqual(english["usage"], {"text": "用于筛选供应商", "original": True})
        # 原文本来就是英文的不算「原文」
        self.assertEqual(english["note"], {"text": "Same supplier", "original": False})
        # 中文界面读原文，英文版不抢位置
        self.assertEqual(chinese["term"], {"text": "准时率", "original": False})
        self.assertEqual(chinese["usage"], {"text": "用于筛选供应商", "original": False})
        for reading in (english, chinese):
            self.assertEqual(reading["empty"], {"text": "", "original": False})
            self.assertEqual(reading["absent"], {"text": "", "original": False})
        self.assertEqual(self.out["localized"]["nothing"], {"text": "", "original": False})

    def test_record_dates_are_only_read_from_real_dates(self):
        dates = self.out["dates"]
        self.assertEqual(dates["plain"], [["2024-01-25", "2024-04-22"]])
        self.assertEqual(dates["withTime"], [["2024-03-30", "2024-03-31"]])
        # 2024-10-12345 是单据号，不是 10 月 12 日
        self.assertEqual(dates["documentNumbers"], [])
        self.assertEqual(dates["impossible"], [])

    # ---- 路由 ----

    def test_unknown_hashes_do_not_navigate(self):
        routes = self.out["routes"]
        for home in ("", "#", "#/", "#/ask"):
            self.assertEqual(routes[home], {"view": "ask"}, home)
        self.assertEqual(routes["#/data"], {"view": "data", "database": "", "table": "", "query": ""})
        self.assertEqual(
            routes["#/data/erp_db"], {"view": "data", "database": "erp_db", "table": "", "query": ""}
        )
        self.assertEqual(
            routes["#/data/erp_db/supplier"],
            {"view": "data", "database": "erp_db", "table": "supplier", "query": ""},
        )
        self.assertEqual(routes["#/history"], {"view": "history"})
        # 页内锚点、不认识的路径、坏的转义：留在原地（null），不是跳去提问页
        for stray in ("#main", "#/nope", "#/data/%E0%A4%A"):
            self.assertIsNone(routes[stray], stray)

    def test_the_query_part_of_a_data_address_is_not_split_into_path_segments(self):
        routes = self.out["routes"]
        # 问号后面是视图自己的状态：里面的斜杠不是路径分隔符
        self.assertEqual(
            routes["#/data/erp_db/supplier?q=a/b&f=c%3Aeq%3A%253F"],
            {"view": "data", "database": "erp_db", "table": "supplier", "query": "q=a/b&f=c%3Aeq%3A%253F"},
        )
        self.assertEqual(
            routes["#/data?tab=fields"], {"view": "data", "database": "", "table": "", "query": "tab=fields"}
        )
        # 表名里真有问号时，链接里它是转义过的，不会被当成查询串的开头
        self.assertEqual(routes["#/data/erp_db/odd%3Fname"]["table"], "odd?name")

    # ---- 数据浏览 ----

    def test_a_table_view_survives_a_round_trip_through_the_address(self):
        route = self.out["viewRoute"]
        # 搜索词、筛选值、字段名里的冒号、问号、斜杠、中文都不影响拆分
        self.assertTrue(route["roundTrip"])
        # 默认的样子不往地址里写东西
        self.assertEqual(route["plain"], "")
        self.assertEqual(route["home"], ["#/data", "#/data/x_db", "#/data"])
        self.assertEqual(route["href"], "#/data/shop%20db/a%2Fb%3Fc?f=k%3Aeq%3AJ-1")

    def test_a_mangled_address_is_read_leniently_not_trusted(self):
        junk = self.out["viewRoute"]["junk"]
        # 认不出的部分丢掉：缺段的条件、不在闭集里的条件、坏的转义、不成立的排序页码页大小
        self.assertEqual(junk["search"], "x")
        self.assertEqual(junk["filters"], [{"column": "ok", "op": "eq", "value": "1"}])
        self.assertIsNone(junk["sort"])
        self.assertEqual((junk["page"], junk["pageSize"], junk["tab"]), (1, 25, "records"))
        # 条数超过服务端的上限也不往下传
        self.assertEqual(self.out["viewRoute"]["tooMany"], 5)

    def test_tables_are_linked_only_by_what_the_catalog_declares(self):
        links = self.out["links"]
        self.assertEqual(links["edges"], 4)
        self.assertEqual(links["groups"], [
            {
                # 连得最多的那个字段是这组键的本家
                "hub": "a_db.orders.order_no",
                "members": ["a_db.orders.order_no", "a_db.items.order_no", "b_db.work.order_no", "c_db.ticket.ref_no"],
                "databases": ["a_db", "b_db", "c_db"],
            },
            {
                # 一样多时取主键那一边
                "hub": "c_db.stock.name",
                "members": ["c_db.ticket.part", "c_db.stock.name"],
                "databases": ["c_db"],
            },
        ])
        # 对端不在目录里（没有权限）的那条线不画，名字也不出现
        self.assertNotIn("z_db", str(links))
        self.assertTrue(links["cached"])
        self.assertEqual(links["empty"], 0)

    def test_related_tables_follow_a_shared_key_transitively(self):
        links = self.out["links"]
        # 工单只登记了和订单的关联；同一个订单号也找得到订单明细和生产记录
        self.assertEqual(links["fromTicket"], [
            ["ref_no", ["a_db.orders.order_no", "a_db.items.order_no", "b_db.work.order_no"]],
            ["part", ["c_db.stock.name"]],
        ])
        self.assertEqual(links["fromStock"], [["name", ["c_db.ticket.part"]]])
        self.assertEqual(links["lonely"], [])
        self.assertEqual(links["across"], [
            ["b_db", ["a_db.orders.order_no>b_db.work.order_no"]],
            ["c_db", ["a_db.orders.order_no>c_db.ticket.ref_no"]],
        ])

    def test_timeline_keeps_empty_months_and_merges_long_spans(self):
        buckets = self.out["buckets"]
        # 没有记录的月份也是一个月；认不出的月份不进时间轴
        self.assertEqual(buckets["gaps"], {"grain": "month", "bars": [
            {"label": "2025-11", "start": "2025-11-01", "end": "2025-12-01", "count": 3},
            {"label": "2025-12", "start": "2025-12-01", "end": "2026-01-01", "count": 0},
            {"label": "2026-01", "start": "2026-01-01", "end": "2026-02-01", "count": 0},
            {"label": "2026-02", "start": "2026-02-01", "end": "2026-03-01", "count": 5},
        ]})
        # 季度按日历对齐：从 2 月开始的数据，第一根柱子仍然是一季度（只含 2、3 两个月）
        quarters = buckets["quarters"]
        self.assertEqual((quarters["grain"], quarters["bars"]), ("quarter", 14))
        self.assertEqual(quarters["first"], {"label": "2020 Q1", "start": "2020-01-01", "end": "2020-04-01", "count": 2})
        self.assertEqual(quarters["last"], {"label": "2023 Q2", "start": "2023-04-01", "end": "2023-07-01", "count": 2})
        years = buckets["years"]
        self.assertEqual((years["grain"], years["bars"]), ("year", 17))
        self.assertEqual(years["first"], {"label": "2000", "start": "2000-01-01", "end": "2001-01-01", "count": 12})
        self.assertEqual(years["last"], {"label": "2016", "start": "2016-01-01", "end": "2017-01-01", "count": 8})
        self.assertEqual(buckets["none"], {"grain": "month", "bars": []})

    def test_value_colours_follow_the_value_not_its_rank(self):
        slots = self.out["slots"]
        self.assertEqual(slots["declared"], [["open", 1], ["waiting", 2], ["closed", 3]])
        # 没声明顺序的按取值本身排（数量会变，颜色不能跟着换）
        self.assertEqual(slots["byValue"], [["9", 1], ["10", 2], ["alpha", 3], ["zeta", 4]])
        # 超过六个取值、或者取值没取全：不上色，不凑第七个颜色
        self.assertEqual((slots["tooMany"], slots["incomplete"], slots["nothing"]), ([], [], []))

    def test_counts_and_shares_are_written_for_reading(self):
        counts = self.out["counts"]
        self.assertEqual(counts["count"], "1,200")
        self.assertEqual(counts["compact"], ["1,200", "47M", "2,356.65", "39.5"])
        # 不是零的一份不显示成 0%
        self.assertEqual(counts["share"], ["<0.1%", "46%", "5%", "0%", ""])

    def test_profiles_are_fetched_once_and_dropped_with_the_catalog(self):
        profiles = self.out["profiles"]
        self.assertEqual(profiles["unsupported"], {"support": "unsupported", "entry": None, "calls": 0})
        self.assertEqual(profiles["off"], {"support": "off", "entry": None})
        # 同一张表问三次（其中一次只要行数）只发一个请求
        self.assertEqual(
            profiles["loaded"],
            {"same": True, "status": "ready", "calls": 1, "count": 7, "neverAsked": None},
        )
        # 换一组条件是另一份；同一组条件不重复取
        self.assertEqual(profiles["filteredCalls"], 2)
        self.assertEqual(profiles["afterReload"], {"count": None, "found": None})

    # ---- 请求层与目录 ----

    def test_an_unreadable_success_body_is_an_error(self):
        self.assertEqual(
            self.out["nonJsonSuccess"], {"name": "Error", "status": 200, "code": "bad_response"}
        )
        self.assertEqual(self.out["abortedBody"]["name"], "AbortError")

    def test_a_late_catalog_response_does_not_overwrite_a_newer_one(self):
        self.assertEqual(self.out["sequencing"], {"identity": "new-identity", "status": "ready"})

    def test_identity_does_not_depend_on_the_catalog(self):
        off = self.out["catalogOff"]
        self.assertEqual((off["status"], off["identity"], off["streaming"]), ("disabled", "buyer", True))
        self.assertEqual(off["calls"], ["/catalog", "/whoami"])

        late = self.out["lateIdentity"]
        self.assertIsNone(late["before"])
        self.assertEqual((late["ensured"], late["identity"], late["status"]), (True, "late", "error"))

    # ---- 模型连接的表单 ----

    def test_the_current_model_key_can_be_kept_only_on_the_same_host(self):
        keep = self.out["modelForm"]["keep"]
        self.assertEqual({name for name, value in keep.items() if value}, {"sameHost", "samePadded"})
        self.assertEqual(self.out["modelForm"]["origins"], [
            "https://relay.example.com", "http://localhost:11434", "http://::1:9000", "", "", "",
        ])

    def test_switching_protocol_fills_in_that_protocols_address_and_models(self):
        anthropic, openai, unknown = self.out["modelForm"]["prefill"]
        self.assertEqual(anthropic, {"provider": "anthropic", "baseUrl": "https://relay.example.com",
                                     "chatModel": "claude-sonnet-5", "coderModel": "claude-opus-5-5"})
        self.assertEqual((openai["baseUrl"], openai["chatModel"]), ("https://api.openai.com/v1", "gpt-5.6-luna"))
        self.assertEqual(unknown, {"provider": "openai", "baseUrl": "", "chatModel": "", "coderModel": ""})

    def test_only_a_page_opened_over_plain_http_from_elsewhere_is_called_insecure(self):
        self.assertEqual(self.out["modelForm"]["insecure"],
                         [False, False, False, False, False, True, True, True, True])

    def test_the_request_carries_trimmed_fields_and_a_blank_key_means_keep(self):
        self.assertEqual(self.out["modelForm"]["body"], {
            "provider": "openai", "base_url": "https://relay.example.com/v1", "api_key": "sk-typed",
            "chat_model": "some-model", "coder_model": "",
        })

    def test_dropdown_choices_are_deduplicated_trimmed_and_sorted(self):
        listed, nothing = self.out["modelForm"]["choices"]
        self.assertEqual(listed, ["claude-x", "GPT-5.6-luna", "gpt-6-luna"])
        self.assertEqual(nothing, [])

    def test_only_a_permanent_rejection_of_the_credential_is_shown_on_the_key(self):
        rejected, model_only, nothing = self.out["modelForm"]["rejection"]
        self.assertEqual(rejected["status_code"], 401)
        self.assertIsNone(model_only)
        self.assertIsNone(nothing)

    # ---- Embedding 的表单 ----

    def test_embedding_form_starts_from_the_remote_in_use_or_the_defaults(self):
        local, remote, unknown = self.out["embeddingForm"]["prefill"]
        self.assertEqual(local, {"baseUrl": "https://api.openai.com/v1", "model": "text-embedding-3-small",
                                 "dimensions": "1024"})
        self.assertEqual(remote, {"baseUrl": "https://relay.example.com/v1", "model": "text-embedding-3-large",
                                  "dimensions": "3072"})
        self.assertEqual(unknown, {"baseUrl": "", "model": "", "dimensions": ""})

    def test_embedding_key_can_be_left_blank_only_for_a_host_that_has_one(self):
        keep = self.out["embeddingForm"]["keep"]
        self.assertEqual({name for name, value in keep.items() if value},
                         {"chatHostOfficial", "blankMeansOfficial", "currentHost"})

    def test_embedding_request_is_trimmed_and_bad_dimensions_mean_default(self):
        typed, blank, fraction = self.out["embeddingForm"]["body"]
        self.assertEqual(typed, {"base_url": "https://api.openai.com/v1", "api_key": "sk-x",
                                 "model": "text-embedding-3-small", "dimensions": 1536})
        self.assertEqual(blank, {"base_url": "", "api_key": "", "model": "", "dimensions": 0})
        self.assertEqual(fraction["dimensions"], 0)


if __name__ == "__main__":
    unittest.main()
