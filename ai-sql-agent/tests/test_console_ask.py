"""
提问页几段逻辑的行为测试：用 Node 直接加载 `api/static/js` 下的真实模块来跑
（做法同 `test_console_logic.py`，没有装 Node 的环境整组跳过）。

提问页上有几样东西是「页面自己拿主意」的，拿错了都不会报错，只会悄悄地误导人：

- 建议问题。它们是拿数据目录拼的，不是写死的——所以要查：目录里没有的表和字段进不了
  建议，读不到的库不出现，换一套目录就换一套问题，而且不拼「合计」这种对比率类指标
  不成立的问法；
- 结果画不画成图。只有形状没有歧义、数字那一列确实是一个量时才画——所以要查：缺值、
  负数、重名、被截断、月份不连续、数字其实是编号的时候不画，而不是画一张看着像回事的图；
- 等待时的进度。只认服务端真实发来的阶段事件——所以要查：没有事件的那条路径
  画不出「已完成」的步骤；
- 「最近问过」。换了身份之后，上一个身份问过的问题不能还留在页面上。

另外，答案里的每一种看法（数字、图、表）都要把来自数据库的文字转义后再输出。
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.api.app import STATIC_DIR

JS_DIR = STATIC_DIR / "js"
NODE = shutil.which("node")

HARNESS = r"""
const JS = process.argv[2];
const load = (name) => import(`${JS}/${name}`);
const out = {};
const { state } = await load('state.js');

// ---- suggest.js：建议问题 -------------------------------------------------------
{
  const { SUGGESTION_KINDS, buildSuggestions, declaredValues, pageCount, pageOf } = await load('suggest.js');
  const column = (name, role, kind, extra = {}) => ({
    name, status: 'available', role, kind, unit: '', value_range: '', aliases: [], ...extra,
  });
  const table = (name, en, zh, columns, status = 'available') => ({
    name, status, label_en: en, label_zh: zh, aliases: [], columns,
  });
  const sales = { database: 'sales_db', status: 'available', tables: [
    table('deal', 'Deals', '成交单', [
      column('deal_code', 'join_key', 'text', { label_en: 'Deal code', label_zh: '成交单号' }),
      column('stage', 'filter', 'text', { label_en: 'Deal stage', label_zh: '成交阶段', value_range: '洽谈 / 签约 / 回款' }),
      column('region', 'dimension', 'text', { label_en: 'Region', label_zh: '区域', value_range: '华东 / 华南' }),
      column('units', 'metric', 'number', { label_en: 'Units', label_zh: '件数' }),
      column('amount', 'metric', 'number', { label_en: 'Deal amount', label_zh: '成交金额', unit: 'USD' }),
      column('closed_on', 'time', 'date', { label_en: 'Closed date', label_zh: '成交日期' }),
      // 数据字典里有、库里没有的字段
      column('ghost', 'filter', 'text', { status: 'missing', label_en: 'Ghost field', label_zh: '幽灵字段', value_range: 'a / b' }),
    ]),
    // 只有自由文字：拼不出任何一种问法
    table('memo', 'Memos', '备忘', [column('body', 'text', 'text', { label_en: 'Body', label_zh: '正文' })]),
    // 数据字典里有、库里没有的表
    table('gone', 'Gone table', '不在的表', [column('kind', 'filter', 'text', { label_en: 'Kind', label_zh: '种类', value_range: 'x / y' })], 'missing'),
  ] };
  const stock = { database: 'stock_db', status: 'available', tables: [
    table('bin', 'PO bins', '库位', [
      column('zone', 'filter', 'text', { label_en: 'Zone', label_zh: '分区', value_range: 'A / B / C' }),
      column('qty', 'metric', 'number', { label_en: 'Quantity', label_zh: '数量' }),
    ]),
  ] };
  // 此刻读不到的库
  const down = { database: 'down_db', status: 'unavailable', tables: [
    table('thing', 'Things', '东西', [column('state', 'filter', 'text', { label_en: 'State', label_zh: '状态', value_range: 'p / q' })]),
  ] };
  // 指标是一个比率：不能相加
  const vendors = { database: 'vendor_db', status: 'available', tables: [
    table('vendor', 'Vendors', '供应商', [
      column('tier', 'dimension', 'text', { label_en: 'Tier', label_zh: '等级', value_range: 'gold / silver' }),
      column('punctuality', 'metric', 'number', { label_en: 'On-time rate', label_zh: '准时率', unit: 'ratio', value_range: '0-1' }),
    ]),
  ] };
  const domains = [sales, stock, down];
  const brief = (list) => list.map((item) => [item.database, item.table, item.kind, item.text]);

  state.lang = 'en';
  const english = buildSuggestions(domains);
  out.suggest = {
    kinds: SUGGESTION_KINDS,
    en: brief(english),
    again: brief(buildSuggestions(domains)),
    ids: english.map((item) => item.id),
    pages: pageCount(english),
    firstPage: brief(pageOf(english, 0)),
    wrapped: brief(pageOf(english, pageCount(english))),
    negative: brief(pageOf(english, -1)),
    lastPage: brief(pageOf(english, pageCount(english) - 1)),
    nothing: brief(buildSuggestions([down, { database: 'bare_db', status: 'available', tables: [sales.tables[1]] }])),
    noCatalog: brief(buildSuggestions(null)),
    emptyPages: [pageCount([]), pageOf([], 3).length],
    ratio: { en: brief(buildSuggestions([vendors])) },
  };
  state.lang = 'zh';
  out.suggest.zh = brief(buildSuggestions(domains));
  out.suggest.ratio.zh = brief(buildSuggestions([vendors]));
  state.lang = 'en';

  // 同一批写法，服务端的 declared_values 也读一遍（见测试的 Python 一侧）
  const inputs = [
    '洽谈 / 签约 / 回款', 'A/B', 'yes / no / yes', '0-1', '正数，单位美元；不含税', '> 0', 'a / / b', 'only', '',
    'trailing /', `${'x'.repeat(120)} / y`, `${'x'.repeat(121)} / y`,
  ];
  out.declared = {
    inputs,
    read: inputs.map((value_range) => declaredValues({ value_range, kind: 'text' })),
    numberField: declaredValues({ value_range: '0 / 1', kind: 'number' }),
    missing: declaredValues(null),
  };
}

// ---- insight.js：画不画成图 ------------------------------------------------------
{
  const { normalizeResult } = await load('model.js');
  const { stepColumns } = await load('steps.js');
  const { chartFor, stepViews } = await load('insight.js');
  const COUNTED = 'SELECT stage, COUNT(*) AS n FROM deal GROUP BY stage';
  const stepOf = (rows, extra = {}) => normalizeResult({
    route: 'database_query', queried_database: true, has_risk: false, degraded: false, execution_status: 'success',
    steps: [{ database: 'sales_db', sql: COUNTED, final_sql: COUNTED, success: true, row_count: rows.length, rows, ...extra }],
  }, { transport: 'query' }).steps[0];
  const describe = (chart) => (chart
    ? { kind: chart.kind, label: chart.label.key, value: chart.value.key, items: chart.items.map((item) => [item.text, item.value]) }
    : null);
  const verdict = (rows, extra) => {
    const step = stepOf(rows, extra);
    const columns = stepColumns(step);
    return { chart: describe(chartFor(step, columns)), views: stepViews(step, columns) };
  };
  const months = (labels) => labels.map((label, index) => ({ month: label, n: index + 1 }));
  const many = (count) => Array.from({ length: count }, (_, index) => ({ name: `item ${index}`, n: index + 1 }));
  out.chart = {
    bars: verdict([{ stage: 'won', n: 3 }, { stage: 'lost', n: 5 }, { stage: 'open', n: 0 }]),
    labelSecond: verdict([{ n: 3, stage: 'won' }, { n: 5, stage: 'lost' }]),
    months: verdict([{ month: '2025-03', n: 2 }, { month: '2025-01', n: 7 }, { month: '2025-02', n: 5 }]),
    yearEnd: verdict(months(['2025-11', '2025-12', '2026-01'])),
    years: verdict([{ y: '2023', n: 1 }, { y: '2024', n: 2 }, { y: '2025', n: 3 }]),
    // 连着的四位数编号不是年份
    storeCodes: verdict([{ store: '1003', n: 9 }, { store: '1001', n: 4 }, { store: '1002', n: 6 }]),
    monthsWithGap: verdict(months(['2025-01', '2025-02', '2025-04'])),
    longGappedSeries: verdict(months(Array.from({ length: 14 }, (_, index) => `20${10 + index}-01`))),
    twoMonths: verdict(months(['2025-01', '2025-02'])),
    twelve: verdict(many(12)),
    thirteen: verdict(many(13)),
    negative: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: -1 }]),
    missingValue: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: null }]),
    missingLabel: verdict([{ stage: 'a', n: 3 }, { stage: null, n: 4 }]),
    duplicateLabel: verdict([{ stage: 'a', n: 3 }, { stage: 'a', n: 4 }]),
    allZero: verdict([{ stage: 'a', n: 0 }, { stage: 'b', n: 0 }]),
    truncated: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }], { truncated: true, row_limit: 2 }),
    threeColumns: verdict([{ stage: 'a', n: 3, m: 1 }, { stage: 'b', n: 4, m: 2 }]),
    twoNumbers: verdict([{ year: 2024, n: 3 }, { year: 2025, n: 4 }]),
    twoTexts: verdict([{ a: 'x', b: 'y' }, { a: 'z', b: 'w' }]),
    numberAsText: verdict([{ stage: 'a', n: '3.50' }, { stage: 'b', n: '4.00' }]),
    // 没有聚合的 SQL、数据字典里也查不到这一列：分不清是量还是编号
    plainSelect: verdict([{ name: 'a', supplier: 3 }, { name: 'b', supplier: 15 }],
      { sql: 'SELECT name, supplier FROM vendor', final_sql: 'SELECT name, supplier FROM vendor' }),
    oneRow: verdict([{ deals: 12, amount: 3400.5 }]),
    oneWideRow: verdict([Object.fromEntries(Array.from({ length: 9 }, (_, index) => [`c${index}`, index]))]),
  };

  // 数据字典里登记过的列：按它的角色定，不看 SQL
  const listed = stepOf([{ name: 'a', amount: 3 }, { name: 'b', amount: 15 }],
    { sql: 'SELECT name, amount FROM deal', final_sql: 'SELECT name, amount FROM deal' });
  const withRole = (role, extra = {}) => describe(chartFor(listed, [
    { key: 'name', numeric: false },
    { key: 'amount', numeric: true, entry: { role }, ...extra },
  ]));
  out.chart.byRole = {
    metric: withRole('metric'),
    metricFilter: withRole('metric_filter'),
    identifier: withRole('join_key'),
    category: withRole('filter'),
    time: withRole('time'),
    noRole: withRole(''),
    primaryKey: withRole('metric', { plain: true }),
  };
}

// ---- process.js：等待时的进度 ----------------------------------------------------
{
  const { stageProgress } = await load('process.js');
  const states = (...names) => stageProgress(names.map((stage) => ({ stage }))).map((step) => `${step.id}:${step.state}`);
  out.progress = {
    none: states(),
    started: states('normalization', 'routing'),
    writing: states('normalization', 'routing', 'retrieval', 'planning', 'generation'),
    // 规划那条进度事件在队列里被丢掉了：规划照样是过去了，不是没发生
    eventDropped: states('normalization', 'routing', 'retrieval', 'generation'),
    repairing: states('normalization', 'routing', 'retrieval', 'planning', 'generation', 'execution', 'repair'),
    checking: states('normalization', 'routing', 'retrieval', 'planning', 'generation', 'execution', 'validation'),
    secondStep: states('retrieval', 'planning', 'generation', 'execution', 'generation'),
    conversation: states('normalization', 'routing', 'answering'),
    unknownStage: states('retrieval', 'something_new'),
    onlyUnknown: states('something_new'),
    missingList: stageProgress(undefined).map((step) => step.state),
  };
}

// ---- format.js：多久以前 --------------------------------------------------------
{
  const { formatRelative } = await load('format.js');
  const now = Date.parse('2026-10-13T12:00:00Z');
  const ago = (seconds) => formatRelative(new Date(now - seconds * 1000).toISOString(), now);
  const read = () => ({
    justNow: ago(10), minutes: ago(5 * 60), hours: ago(3 * 3600), yesterday: ago(26 * 3600),
    days: ago(4 * 86400), older: ago(9 * 86400), clockSkew: ago(-20),
  });
  state.lang = 'en';
  out.relative = { en: read(), invalid: formatRelative('not a date', now), empty: formatRelative('', now) };
  state.lang = 'zh';
  out.relative.zh = read();
  state.lang = 'en';
}

// ---- historyStore.js：换了身份，手里的历史作废 ------------------------------------
{
  let handler = null;
  globalThis.fetch = (url, options) => handler(String(url), options);
  const json = (status, body) => Promise.resolve({ ok: status < 400, status, json: async () => body });
  const { history, loadHistory, resetHistory } = await load('historyStore.js');
  const snapshot = () => ({ status: history.status, queries: history.items.map((item) => item.query), loaded: Boolean(history.loadedAt) });
  const items = (...queries) => ({ items: queries.map((query, index) => ({ trace_id: `t${index}`, query })) });

  handler = () => json(200, items('first identity question'));
  await loadHistory();
  const loaded = snapshot();

  // 之后一次刷新失败：手里那份还在
  handler = () => json(429, { detail: 'slow down' });
  await loadHistory();
  const afterFailure = snapshot();

  resetHistory();
  const afterReset = snapshot();

  // 换身份之前发出去的请求，换完才回来：不能把上一个身份的问题填回来
  handler = () => json(200, items('first identity question'));
  await loadHistory();
  let release;
  handler = () => new Promise((resolve) => { release = () => resolve(json(200, items('stale answer for the old identity'))); });
  const stale = loadHistory();
  resetHistory();
  handler = () => json(200, items('second identity question'));
  const fresh = loadHistory();
  release();
  await stale;
  await fresh;
  out.history = { loaded, afterFailure, afterReset, afterSwitch: snapshot() };
}

// ---- result.js：答案的标记（拼字符串，不需要 DOM） --------------------------------
{
  const { catalog } = await load('catalog.js');
  const { normalizeResult } = await load('model.js');
  const { renderPending, renderResult, renderServiceError, renderStopped, summaryText } = await load('result.js');
  const { newTurnUi } = await load('turnActions.js');
  const EVIL = '<img src=x onerror=alert(1)>';
  const COUNTED = 'SELECT stage, COUNT(*) AS n FROM deal GROUP BY stage';
  const turnOf = (payload) => ({
    id: 't1', question: 'How many?', status: 'done', cached: false, rerunOf: null, ui: newTurnUi(),
    result: normalizeResult({
      trace_id: 'trace-1', route: 'database_query', queried_database: true, has_risk: false, degraded: false,
      execution_status: 'success', ...payload,
    }, { transport: 'query', clientMs: 1234 }),
  });
  const step = (rows, extra = {}) => ({
    database: 'sales_db', sql: COUNTED, final_sql: COUNTED, success: true, row_count: rows.length, rows, ...extra,
  });
  const facts = (markup) => ({
    figures: markup.includes('class="stat-tiles"'),
    bars: markup.includes('class="viz-list has-bars"'),
    columns: markup.includes('class="viz-cols is-static"'),
    table: markup.includes('class="data-table"'),
    switcher: (markup.match(/data-action="step-view"/g) || []).length,
    pressed: (markup.match(/data-which="(\w+)"\s+aria-pressed="true"/) || [])[1] || '',
    legend: markup.includes('data-detail="legend-0"'),
    raw: markup.includes(EVIL),
    escaped: markup.includes('&lt;img src=x onerror=alert(1)&gt;'),
  });
  const render = (turn) => facts(String(renderResult(turn)));

  const chartTurn = turnOf({ steps: [step([{ stage: EVIL, n: 3 }, { stage: 'b"c', n: 5 }])] });
  const chartMarkup = String(renderResult(chartTurn));
  const chartDefault = facts(chartMarkup);
  chartTurn.ui.views[0] = 'table';
  const chartAsTable = render(chartTurn);
  chartTurn.ui.views[0] = 'nonsense';
  const chartBadChoice = render(chartTurn);

  const oneRowTurn = turnOf({ steps: [step([{ customer: EVIL, total: 12.5 }], { column_units: { total: 'USD' } })] });
  const figuresDefault = render(oneRowTurn);
  const figuresMarkup = String(renderResult(oneRowTurn));
  oneRowTurn.ui.views[0] = 'table';
  const figuresAsTable = render(oneRowTurn);

  const wide = render(turnOf({ steps: [step([{ a: EVIL, b: 1, c: 2 }, { a: 'x', b: 3, c: 4 }])] }));
  const truncated = turnOf({ result_truncated: true, steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }], { truncated: true, row_limit: 2 })] });

  // 连着的月份：柱子和表格一起出，不是二选一
  const monthly = (rows) => String(renderResult(turnOf({ steps: [step(rows)] })));
  const series = monthly([{ month: '2025-01', n: 7 }, { month: '2025-02', n: 12 }, { month: '2025-03', n: 9 }]);
  const tied = monthly([{ month: '2025-01', n: 12 }, { month: '2025-02', n: 12 }, { month: '2025-03', n: 9 }]);
  const allTied = monthly([{ month: '2025-01', n: 5 }, { month: '2025-02', n: 5 }, { month: '2025-03', n: 5 }, { month: '2025-04', n: 5 }]);
  const note = (markup) => (markup.match(/<p class="chart-note">([^<]*)<\/p>/) || [])[1] || '';

  // 有一步没有返回行：它查的那个库不能从页面上消失
  const emptyStep = String(renderResult(turnOf({ steps: [
    step([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }]),
    step([], { database: 'stock_db', sql: 'SELECT zone, qty FROM bin WHERE qty < 0', final_sql: 'SELECT zone, qty FROM bin WHERE qty < 0' }),
  ] })));
  const metaOf = (markup) => (markup.match(/<div class="result-meta">([\s\S]*?)<\/div>/) || [])[1] || '';

  // 数据字典里登记过的列：图按角色定，列说明在每一种看法下都有
  catalog.status = 'ready';
  catalog.data = { domains: [{ database: 'sales_db', status: 'available', kind: '', aliases: [], title: { en: 'Sales' }, tables: [{
    name: 'deal', status: 'available', aliases: [], columns: [
      { name: 'stage', status: 'available', role: 'filter', kind: 'text', aliases: [], description: 'Where the deal stands.' },
      { name: 'amount', status: 'available', role: 'metric', kind: 'number', unit: 'USD', aliases: [], description: 'Agreed price.' },
      { name: 'seq', status: 'available', role: 'join_key', kind: 'number', aliases: [], description: 'Running number.' },
    ],
  }] }] };
  const plain = { sql: 'SELECT stage, amount FROM deal', final_sql: 'SELECT stage, amount FROM deal' };
  const listedTurn = turnOf({ steps: [step([{ stage: 'won', amount: 300 }, { stage: 'lost', amount: 120 }], plain)] });
  const listedChart = render(listedTurn);
  listedTurn.ui.views[0] = 'table';
  const listedTable = render(listedTurn);
  const identifiers = render(turnOf({ steps: [step([{ stage: 'won', seq: 7 }, { stage: 'lost', seq: 8 }],
    { sql: 'SELECT stage, seq FROM deal', final_sql: 'SELECT stage, seq FROM deal' })] }));
  const listedFigures = render(turnOf({ steps: [step([{ stage: 'won', amount: 300 }], plain)] }));
  catalog.status = 'idle';
  catalog.data = null;

  const chat = String(renderResult(turnOf({ route: 'data_qa', queried_database: false, execution_status: 'not_applicable', answer: `**hi** ${EVIL}`, steps: [] })));
  const pending = (transport, stages = []) => String(renderPending({
    transport, stages, stageNow: stages[stages.length - 1] || null, ui: newTurnUi(),
  }, 5));
  const streamed = pending('a2a-stream', [{ stage: 'normalization' }, { stage: 'retrieval' }]);
  const plainWait = pending('query');

  out.answer = {
    chartDefault, chartAsTable, chartBadChoice, figuresDefault, figuresAsTable, wide,
    quotedTitle: chartMarkup.includes('title="b&quot;c"') && !chartMarkup.includes('title="b"c"'),
    figuresUnit: figuresMarkup.includes('<span class="stat-unit">USD</span>'),
    truncated: render(truncated),
    truncatedCopy: summaryText(truncated),
    series: facts(series),
    seriesNote: note(series),
    tiedNote: note(tied),
    allTiedNote: note(allTied),
    emptyStep: { panels: (emptyStep.match(/class="result-panel /g) || []).length, meta: metaOf(emptyStep) },
    onePanelMeta: metaOf(chartMarkup),
    listedChart, listedTable, identifiers, listedFigures,
    chat: { panel: chat.includes('result-panel'), raw: chat.includes(EVIL), bold: chat.includes('<strong>hi</strong>') },
    streamed: { track: streamed.includes('stage-track'), bar: streamed.includes('progress-indet'), now: streamed.includes('stage is-now'), done: streamed.includes('stage is-done') },
    plain: { track: plainWait.includes('stage-track'), bar: plainWait.includes('progress-indet'), done: plainWait.includes('is-done') },
    stop: [streamed, plainWait].every((markup) => markup.includes('data-action="stop"')),
    stopped: String(renderStopped()).includes('data-action="ask-again"'),
    failure: (() => {
      const markup = String(renderServiceError({ error: { status: 401, serverMessage: EVIL }, ui: newTurnUi() }));
      return { settings: markup.includes('data-action="open-settings"'), raw: markup.includes(EVIL) };
    })(),
  };
}

console.log(JSON.stringify(out));
"""


def dictionary(language: str) -> dict:
    text = (JS_DIR / "i18n" / f"{language}.js").read_text(encoding="utf-8")
    return dict(re.findall(r"^\s*'([^']+)':\s*'((?:[^'\\]|\\.)*)',?\s*$", text, flags=re.M))


@unittest.skipUnless(NODE, "没有安装 Node：跳过提问页逻辑的行为测试")
class AskPageLogicTest(unittest.TestCase):
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

    # ---- 建议问题 ----

    def test_suggestions_are_built_from_the_catalog_and_nothing_else(self):
        english = self.out["suggest"]["en"]
        texts = {(database, table, kind): text for database, table, kind, text in english}
        self.assertEqual(texts[("sales_db", "deal", "countBy")], "Count deals by deal stage")
        # 两个数值字段里挑带单位的那个（金额），不是排在前面的那个
        self.assertEqual(texts[("sales_db", "deal", "topBy")], "Top 10 deals by deal amount")
        self.assertEqual(texts[("sales_db", "deal", "perMonth")], "Count deals per month, by closed date")
        self.assertEqual(texts[("sales_db", "deal", "averageBy")], "Average deal amount of deals by deal stage")
        # 「筛出一个取值」换一个分类字段，取它声明的第一个取值（取值是数据，原样写）
        self.assertEqual(texts[("sales_db", "deal", "whereIs")], "List deals where region is “华东”")
        # 只有一个分类字段时就用它；全大写的缩写不改成小写
        self.assertEqual(texts[("stock_db", "bin", "whereIs")], "List PO bins where zone is “A”")
        self.assertEqual(texts[("stock_db", "bin", "topBy")], "Top 10 PO bins by quantity")
        # 没有日期字段就没有「按月」
        self.assertNotIn(("stock_db", "bin", "perMonth"), texts)
        self.assertEqual(len(english), 9)

    def test_nothing_unreadable_or_undescribed_is_suggested(self):
        suggest = self.out["suggest"]
        everything = json.dumps(suggest["en"] + suggest["zh"], ensure_ascii=False)
        # 读不到的库、库里没有的表和字段、只有自由文字的表，都拼不出建议
        for absent in ("down_db", "Things", "gone", "Gone table", "Ghost field", "幽灵字段", "memo", "Memos"):
            self.assertNotIn(absent, everything, absent)
        self.assertEqual(suggest["nothing"], [])
        self.assertEqual(suggest["noCatalog"], [])

    def test_no_suggestion_adds_up_a_metric(self):
        # 数据字典只说「这是个指标」，没说它能不能相加：准时率、单价加起来没有意义
        ratio = self.out["suggest"]["ratio"]
        self.assertEqual(
            sorted(text for _, _, _, text in ratio["en"]),
            ["Average on-time rate of vendors by tier", "Count vendors by tier",
             "List vendors where tier is “gold”", "Top 10 vendors by on-time rate"],
        )
        everything = self.out["suggest"]["en"] + self.out["suggest"]["zh"] + ratio["en"] + ratio["zh"]
        for _, _, _, text in everything:
            for word in ("Total", "Sum", "合计", "汇总", "总和"):
                self.assertNotIn(word, text)
        for language in ("en", "zh"):
            self.assertNotIn("suggest.sumBy", dictionary(language))

    def test_the_order_is_stable_and_the_first_page_is_varied(self):
        suggest = self.out["suggest"]
        self.assertEqual(suggest["en"], suggest["again"])
        self.assertEqual(len(set(suggest["ids"])), len(suggest["ids"]))
        # 第一轮每个库一条，而且问法不重样
        first = suggest["en"][:2]
        self.assertEqual([item[0] for item in first], ["sales_db", "stock_db"])
        self.assertEqual(len({item[2] for item in first}), 2)
        self.assertEqual(sorted({item[2] for item in suggest["en"]}), sorted(suggest["kinds"]))

    def test_paging_wraps_around(self):
        suggest = self.out["suggest"]
        self.assertEqual(suggest["pages"], 3)
        self.assertEqual(suggest["firstPage"], suggest["en"][:4])
        self.assertEqual(suggest["wrapped"], suggest["firstPage"])
        self.assertEqual(suggest["negative"], suggest["lastPage"])
        self.assertEqual(suggest["lastPage"], suggest["en"][8:])
        self.assertEqual(suggest["emptyPages"], [1, 0])

    def test_suggestions_follow_the_interface_language(self):
        chinese = {(database, table, kind): text for database, table, kind, text in self.out["suggest"]["zh"]}
        self.assertEqual(chinese[("sales_db", "deal", "countBy")], "按成交阶段统计成交单数量")
        self.assertEqual(chinese[("sales_db", "deal", "topBy")], "成交金额最高的 10 条成交单")
        self.assertEqual(chinese[("sales_db", "deal", "perMonth")], "按月统计成交单数量（按成交日期）")
        self.assertEqual(chinese[("sales_db", "deal", "averageBy")], "按成交阶段看成交单的平均成交金额")
        self.assertEqual(chinese[("sales_db", "deal", "whereIs")], "列出区域为「华东」的成交单")
        self.assertEqual(len(chinese), len(self.out["suggest"]["en"]))

    def test_declared_values_are_read_exactly_as_the_server_reads_them(self):
        from ai_sql_agent.catalog.profile import declared_values

        declared = self.out["declared"]
        for text, read in zip(declared["inputs"], declared["read"], strict=True):
            self.assertEqual(read, list(declared_values(text)), text)
        by_input = dict(zip(declared["inputs"], declared["read"]))
        self.assertEqual(by_input["洽谈 / 签约 / 回款"], ["洽谈", "签约", "回款"])
        self.assertEqual(by_input["yes / no / yes"], ["yes", "no"])
        # 范围、一句话、比较式都不是清单
        for text in ("0-1", "正数，单位美元；不含税", "> 0", "a / / b", "only", "", "trailing /"):
            self.assertEqual(by_input[text], [], text)
        # 页面比服务端多的一条：数字字段上的清单不拿来拼问题
        self.assertEqual(declared["numberField"], [])
        self.assertEqual(declared["missing"], [])

    def test_every_question_form_and_view_has_its_wording(self):
        english, chinese = dictionary("en"), dictionary("zh")
        for kind in self.out["suggest"]["kinds"]:
            for language in (english, chinese):
                template = language[f"suggest.{kind}"]
                # 每一种问法里都有表名的位置：没有它，那就是一句写死的问题了
                self.assertIn("{table}", template, kind)
        for step in ("understand", "retrieve", "plan", "generate", "execute", "check"):
            self.assertIn(f"step.{step}", english, step)
        for view in ("figures", "chart", "table"):
            self.assertIn(f"view.{view}", english, view)

    def test_the_page_carries_no_written_out_example_question(self):
        # 以前的示例问题是写死在字典里的，换了一套库之后它们问的就是不存在的数据
        for language in ("en", "zh"):
            self.assertEqual([key for key in dictionary(language) if key.startswith("example.")], [])
        ask = (JS_DIR / "views" / "ask.js").read_text(encoding="utf-8")
        self.assertNotIn("EXAMPLES", ask)
        self.assertIn("buildSuggestions", ask)

    # ---- 画不画成图 ----

    def test_a_name_and_a_counted_number_become_bars_in_the_returned_order(self):
        chart = self.out["chart"]
        self.assertEqual(
            chart["bars"]["chart"],
            {"kind": "bars", "label": "stage", "value": "n", "items": [["won", 3], ["lost", 5], ["open", 0]]},
        )
        self.assertEqual(chart["bars"]["views"], ["chart", "table"])
        # 哪一列在前不要紧
        self.assertEqual(chart["labelSecond"]["chart"]["label"], "stage")
        self.assertEqual(len(chart["twelve"]["chart"]["items"]), 12)

    def test_an_unbroken_run_of_months_or_years_becomes_columns_shown_with_the_table(self):
        chart = self.out["chart"]
        self.assertEqual(chart["months"]["chart"]["kind"], "columns")
        self.assertEqual(
            chart["months"]["chart"]["items"], [["2025-01", 7], ["2025-02", 5], ["2025-03", 2]]
        )
        self.assertEqual(chart["yearEnd"]["chart"]["kind"], "columns")
        self.assertEqual(chart["years"]["chart"]["kind"], "columns")
        # 柱子上写不下数字：它不是一种可以单独切过去的看法，只和表格一起出现
        self.assertEqual(chart["months"]["views"], ["table"])
        series = self.out["answer"]["series"]
        self.assertEqual((series["columns"], series["table"], series["switcher"]), (True, True, 0))

    def test_four_digit_codes_are_not_years(self):
        store_codes = self.out["chart"]["storeCodes"]["chart"]
        self.assertEqual(store_codes["kind"], "bars")
        # 按查询返回的顺序画，不按编号重排
        self.assertEqual([label for label, _ in store_codes["items"]], ["1003", "1001", "1002"])

    def test_a_series_with_a_missing_month_is_not_drawn_as_adjacent_columns(self):
        chart = self.out["chart"]
        # 缺了 3 月：不能把 2 月和 4 月贴在一起画。行数少就退回横条，多了就不画
        self.assertEqual(chart["monthsWithGap"]["chart"]["kind"], "bars")
        self.assertIsNone(chart["longGappedSeries"]["chart"])
        self.assertEqual(chart["twoMonths"]["chart"]["kind"], "bars")

    def test_ambiguous_or_incomplete_results_get_a_table_only(self):
        chart = self.out["chart"]
        for name in (
            "thirteen", "negative", "missingValue", "missingLabel", "duplicateLabel", "allZero",
            "truncated", "threeColumns", "twoNumbers", "twoTexts", "numberAsText", "plainSelect",
        ):
            self.assertIsNone(chart[name]["chart"], name)
            self.assertEqual(chart[name]["views"], ["table"], name)

    def test_a_number_is_only_charted_when_it_is_a_quantity(self):
        by_role = self.out["chart"]["byRole"]
        # 数据字典里登记成指标的列：没有聚合也画
        self.assertEqual(by_role["metric"]["kind"], "bars")
        self.assertEqual(by_role["metricFilter"]["kind"], "bars")
        # 编号、分类、日期也可能是数字，但不是量；主键即使标成指标也不画
        for name in ("identifier", "category", "time", "noRole", "primaryKey"):
            self.assertIsNone(by_role[name], name)
        answer = self.out["answer"]
        self.assertEqual((answer["listedChart"]["bars"], answer["listedChart"]["pressed"]), (True, "chart"))
        self.assertEqual((answer["identifiers"]["bars"], answer["identifiers"]["table"]), (False, True))

    def test_a_single_row_is_shown_as_figures(self):
        chart = self.out["chart"]
        self.assertEqual(chart["oneRow"]["views"], ["figures", "table"])
        self.assertIsNone(chart["oneRow"]["chart"])
        # 列太多时一排大数字反而难读
        self.assertEqual(chart["oneWideRow"]["views"], ["table"])

    def test_the_highest_value_note_names_every_tied_period_or_says_nothing(self):
        answer = self.out["answer"]
        self.assertEqual(answer["seriesNote"], "Highest: 2025-02 (12)")
        self.assertEqual(answer["tiedNote"], "Highest: 2025-01, 2025-02 (12)")
        self.assertEqual(answer["allTiedNote"], "")

    # ---- 等待时的进度 ----

    def test_progress_only_reflects_stage_events_that_arrived(self):
        progress = self.out["progress"]
        todo = ["understand:todo", "retrieve:todo", "plan:todo", "generate:todo", "execute:todo", "check:todo"]
        self.assertEqual(progress["none"], todo)
        self.assertEqual(progress["onlyUnknown"], todo)
        self.assertEqual(progress["missingList"], ["todo"] * 6)
        self.assertEqual(progress["started"], ["understand:now"] + todo[1:])
        self.assertEqual(progress["conversation"], ["understand:now"] + todo[1:])
        writing = ["understand:done", "retrieve:done", "plan:done", "generate:now", "execute:todo", "check:todo"]
        self.assertEqual(progress["writing"], writing)
        # 进度事件会在队列满时被丢掉：前面某一步没收到事件，不能画成「没发生」
        self.assertEqual(progress["eventDropped"], writing)
        self.assertEqual(progress["repairing"][4:], ["execute:now", "check:todo"])
        self.assertEqual(progress["checking"][-1], "check:now")
        # 多步查询里第二步又回到写 SQL：进度跟着最后一条事件走
        self.assertEqual(progress["secondStep"][3:], ["generate:now", "execute:todo", "check:todo"])
        self.assertEqual(progress["unknownStage"][:2], ["understand:done", "retrieve:now"])
        for states in progress.values():
            for state in states:
                self.assertIn(state.split(":")[-1], ("done", "now", "todo"))

    def test_waiting_without_stage_events_shows_no_steps(self):
        answer = self.out["answer"]
        self.assertEqual(answer["streamed"], {"track": True, "bar": False, "now": True, "done": True})
        # POST /query 什么都不回报：只有一条不表示进度的动效，没有「已完成」的步骤
        self.assertEqual(answer["plain"], {"track": False, "bar": True, "done": False})
        self.assertTrue(answer["stop"])
        self.assertTrue(answer["stopped"])

    # ---- 多久以前、最近问过 ----

    def test_recent_times_are_relative_and_old_ones_are_dates(self):
        english, chinese = self.out["relative"]["en"], self.out["relative"]["zh"]
        self.assertEqual(english["justNow"], "now")
        self.assertEqual(english["clockSkew"], "now")
        self.assertEqual(english["minutes"], "5 minutes ago")
        self.assertEqual(english["hours"], "3 hours ago")
        self.assertEqual(english["yesterday"], "yesterday")
        self.assertEqual(english["days"], "4 days ago")
        self.assertEqual(english["older"], "Oct 4, 2026")
        self.assertEqual(chinese["minutes"], "5分钟前")
        self.assertEqual(chinese["yesterday"], "昨天")
        self.assertIn("2026", chinese["older"])
        self.assertEqual(self.out["relative"]["invalid"], "not a date")
        self.assertEqual(self.out["relative"]["empty"], "")

    def test_history_is_dropped_when_the_identity_changes(self):
        history = self.out["history"]
        self.assertEqual(
            history["loaded"], {"status": "ready", "queries": ["first identity question"], "loaded": True}
        )
        # 一次刷新失败：状态是出错，但取到过的那份还在，首页的「最近问过」不至于整段消失
        self.assertEqual(
            history["afterFailure"], {"status": "error", "queries": ["first identity question"], "loaded": True}
        )
        self.assertEqual(history["afterReset"], {"status": "idle", "queries": [], "loaded": False})
        # 换身份之前发出去、之后才回来的那次请求不算数
        self.assertEqual(
            history["afterSwitch"], {"status": "ready", "queries": ["second identity question"], "loaded": True}
        )

    # ---- 答案的几种看法 ----

    def test_each_result_opens_in_its_clearest_view_and_can_switch_to_the_table(self):
        answer = self.out["answer"]
        self.assertEqual(
            (answer["chartDefault"]["bars"], answer["chartDefault"]["table"], answer["chartDefault"]["pressed"]),
            (True, False, "chart"),
        )
        self.assertEqual(answer["chartDefault"]["switcher"], 2)
        self.assertEqual(
            (answer["chartAsTable"]["bars"], answer["chartAsTable"]["table"], answer["chartAsTable"]["pressed"]),
            (False, True, "table"),
        )
        # 记下来的选择对不上任何一种看法时，退回默认的那种
        self.assertEqual(answer["chartBadChoice"]["pressed"], "chart")
        self.assertEqual(
            (answer["figuresDefault"]["figures"], answer["figuresDefault"]["table"]), (True, False)
        )
        self.assertTrue(answer["figuresUnit"])
        self.assertTrue(answer["figuresAsTable"]["table"])
        # 只有表格这一种看法时不画开关
        self.assertEqual((answer["wide"]["table"], answer["wide"]["switcher"]), (True, 0))

    def test_the_column_legend_is_available_in_every_view(self):
        answer = self.out["answer"]
        for name in ("listedChart", "listedTable", "listedFigures"):
            self.assertTrue(answer[name]["legend"], name)
        # 数据字典里一列都对不上时没有说明可给
        self.assertFalse(answer["chartDefault"]["legend"])

    def test_a_queried_database_stays_visible_when_its_step_returned_nothing(self):
        answer = self.out["answer"]
        self.assertEqual(answer["emptyStep"]["panels"], 1)
        self.assertIn("stock_db", answer["emptyStep"]["meta"])
        self.assertIn("sales_db", answer["emptyStep"]["meta"])
        # 每一步都有面板时来源已经写在面板头上，不在下面重复一遍
        self.assertNotIn("sales_db", answer["onePanelMeta"])

    def test_a_truncated_result_is_never_charted_and_the_copy_says_so(self):
        answer = self.out["answer"]
        self.assertEqual((answer["truncated"]["bars"], answer["truncated"]["table"]), (False, True))
        self.assertIn("Not all rows are shown", answer["truncatedCopy"])

    def test_database_text_is_escaped_in_every_view(self):
        answer = self.out["answer"]
        for name in ("chartDefault", "chartAsTable", "figuresDefault", "figuresAsTable", "wide"):
            self.assertFalse(answer[name]["raw"], name)
            self.assertTrue(answer[name]["escaped"], name)
        # 属性里的引号也转义（图里每一行的名字还放在 title 里）
        self.assertTrue(answer["quotedTitle"])
        self.assertEqual(answer["chat"], {"panel": False, "raw": False, "bold": True})
        self.assertEqual(answer["failure"], {"settings": True, "raw": False})


if __name__ == "__main__":
    unittest.main()
