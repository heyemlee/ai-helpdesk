/**
 * 问数页的几段纯逻辑：等待时的进度、跨库里哪一步是中间步骤、多久以前、对话回答的排版、
 * 学习模式引用的源码都还在（移植自 test_console_ask.py、test_console_logic.py、test_console_frontend.py）。
 */
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { LEARN_SOURCES } from '@/components/data/ask/Process'
import { stageProgress } from '../ask-progress'
import { formatRelative } from '../format'
import { normalizeResult, passedOn, splitSteps } from '../model'
import { parseRichText } from '../richtext'

describe('等待时的进度只认真正收到的阶段事件', () => {
  const states = (...names: string[]) => stageProgress(names.map((stage) => ({ stage }))).map((step) => `${step.id}:${step.state}`)
  const todo = ['understand:todo', 'retrieve:todo', 'plan:todo', 'generate:todo', 'execute:todo', 'check:todo']

  it('没有事件、只有认不出的事件：一步都没开始', () => {
    expect(states()).toEqual(todo)
    expect(states('something_new')).toEqual(todo)
    expect(stageProgress(undefined).map((step) => step.state)).toEqual(Array(6).fill('todo'))
  })

  it('最后一条事件那一步是"正在做"，前面的都过去了——被丢掉的事件不算没发生', () => {
    expect(states('normalization', 'routing')).toEqual(['understand:now', ...todo.slice(1)])
    expect(states('normalization', 'routing', 'answering')).toEqual(['understand:now', ...todo.slice(1)])
    const writing = ['understand:done', 'retrieve:done', 'plan:done', 'generate:now', 'execute:todo', 'check:todo']
    expect(states('normalization', 'routing', 'retrieval', 'planning', 'generation')).toEqual(writing)
    // 规划那条进度事件在队列里被丢掉了：规划照样是过去了
    expect(states('normalization', 'routing', 'retrieval', 'generation')).toEqual(writing)
    expect(states('normalization', 'routing', 'retrieval', 'planning', 'generation', 'execution', 'repair').slice(4))
      .toEqual(['execute:now', 'check:todo'])
    expect(states('normalization', 'routing', 'retrieval', 'planning', 'generation', 'execution', 'validation').at(-1))
      .toBe('check:now')
    // 多步查询里第二步又回到写 SQL：进度跟着最后一条事件走
    expect(states('retrieval', 'planning', 'generation', 'execution', 'generation').slice(3))
      .toEqual(['generate:now', 'execute:todo', 'check:todo'])
    expect(states('retrieval', 'something_new').slice(0, 2)).toEqual(['understand:done', 'retrieve:now'])
  })
})

describe('跨库查询里哪一步是中间步骤', () => {
  const make = (steps: unknown[]) => normalizeResult({
    route: 'database_query', queried_database: true, has_risk: false, degraded: false, execution_status: 'success', steps,
  }, { transport: 'query' })
  const step = (database: string, sql: string, rows: Array<Record<string, unknown>>) =>
    ({ database, sql, final_sql: sql, success: true, row_count: rows.length, rows })
  const verdict = (result: ReturnType<typeof make>, question = '') => {
    const split = splitSteps(result, question)
    return {
      answers: split.answers.map((item) => item.index + 1),
      intermediate: split.intermediate.map((item) => item.index + 1),
      carried: passedOn(result, result.steps[0]!, question).map((item) => [item.column, item.values.length, item.intoStep + 1]),
    }
  }

  it('一步的每一列取值都写进了下一步的 SQL：它是中间步骤', () => {
    const handoff = verdict(make([
      step('pm_db', 'SELECT "project_code" FROM "project" WHERE "actual_cost" > "budget_amt";',
        [{ project_code: 'J24001' }, { project_code: 'J24003' }, { project_code: 'J24006' }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) AS total FROM purchase_order WHERE job_no IN ('J24001', 'J24003', 'J24006') GROUP BY job_no;",
        [{ job_no: 'J24001', total: 127200 }]),
    ]))
    expect(handoff).toEqual({ answers: [2], intermediate: [1], carried: [['project_code', 3, 2]] })
  })

  it('数字键不带引号也认得出（Python 的 1200.0 到了页面是 1200）', () => {
    expect(verdict(make([
      step('erp_db', 'SELECT supplier_id FROM supplier WHERE on_time_rate < 0.9',
        [{ supplier_id: 3 }, { supplier_id: 15 }, { supplier_id: 27 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id IN (3, 15, 27)', [{ batch_no: 'B1' }]),
    ])).intermediate).toEqual([1])
    expect(verdict(make([
      step('erp_db', "SELECT supplier_id FROM supplier WHERE supplier_name = 'Canyon Crest Quartz'", [{ supplier_id: 1042 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id = 1042', [{ batch_no: 'B1' }]),
    ])).intermediate).toEqual([1])
    expect(verdict(make([
      step('a', 'SELECT amount FROM t', [{ amount: 1200 }, { amount: 88.5 }]),
      step('b', 'SELECT x FROM u WHERE amount IN (1200.0, 88.5)', [{ x: 1 }]),
    ])).intermediate).toEqual([1])
  })

  it('还带着别的列的步骤留在答案里：那些列是答案的一部分', () => {
    const extras = verdict(make([
      step('pm_db', 'SELECT project_code, project_name, actual_cost FROM project WHERE actual_cost > budget_amt',
        [{ project_code: 'J24001', project_name: 'Maple Kitchen', actual_cost: 91000 },
          { project_code: 'J24003', project_name: 'Quartz Bath', actual_cost: 45000 }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) t FROM purchase_order WHERE job_no IN ('J24001', 'J24003') GROUP BY job_no",
        [{ job_no: 'J24001', t: 1 }]),
    ]))
    expect([extras.answers, extras.intermediate]).toEqual([[1, 2], []])
  })

  it('巧合不算交接：同一个筛选值、问题里写着的值、LIMIT 后的数字、别的数字里的一段', () => {
    const cases = {
      sharedFilter: verdict(make([
        step('erp_db', "SELECT order_no, status FROM sales_order WHERE status = 'open'",
          [{ order_no: 'SO-1', status: 'open' }, { order_no: 'SO-2', status: 'open' }]),
        step('mes_db', "SELECT wo_no, status FROM work_order WHERE status = 'open'", [{ wo_no: 'WO-9', status: 'open' }]),
      ])),
      valueFromQuestion: verdict(make([
        step('pm_db', "SELECT project_code FROM project WHERE project_code = 'J24001'", [{ project_code: 'J24001' }]),
        step('erp_db', "SELECT po_no FROM purchase_order WHERE job_no = 'J24001'", [{ po_no: 'PO-1' }]),
      ]), 'Show purchase orders for project J24001'),
      loneNumberElsewhere: verdict(make([
        step('erp_db', 'SELECT COUNT(*) AS n FROM supplier', [{ n: 3 }]),
        step('wms_db', 'SELECT batch_no FROM batch ORDER BY received_on DESC LIMIT 3', [{ batch_no: 'B1' }]),
      ])),
      digitsInsideOtherNumbers: verdict(make([
        step('a', 'SELECT id FROM t', [{ id: 12 }, { id: 7 }]),
        step('b', 'SELECT x FROM u WHERE id IN (123, 77) AND rate > 1.12', [{ x: 1 }]),
      ])),
    }
    for (const [name, result] of Object.entries(cases)) {
      expect(result.intermediate, name).toEqual([])
      expect(result.answers, name).toEqual([1, 2])
    }
    for (const name of ['valueFromQuestion', 'loneNumberElsewhere', 'digitsInsideOtherNumbers'] as const) {
      expect(cases[name].carried, name).toEqual([])
    }
  })
})

describe('多久以前', () => {
  const now = Date.parse('2026-10-13T12:00:00Z')
  const ago = (seconds: number, lang: 'en' | 'zh') => formatRelative(new Date(now - seconds * 1000).toISOString(), lang, now)

  it('一周以内说相对时间，再早写日期；读不出的原样给', () => {
    expect(ago(10, 'en')).toBe('now')
    expect(ago(-20, 'en')).toBe('now')
    expect(ago(5 * 60, 'en')).toBe('5 minutes ago')
    expect(ago(3 * 3600, 'en')).toBe('3 hours ago')
    expect(ago(26 * 3600, 'en')).toBe('yesterday')
    expect(ago(4 * 86400, 'en')).toBe('4 days ago')
    expect(ago(9 * 86400, 'en')).toBe('Oct 4, 2026')
    expect(ago(5 * 60, 'zh')).toBe('5分钟前')
    expect(ago(26 * 3600, 'zh')).toBe('昨天')
    expect(ago(9 * 86400, 'zh')).toContain('2026')
    expect(formatRelative('not a date', 'en', now)).toBe('not a date')
    expect(formatRelative('', 'en', now)).toBe('')
  })
})

describe('对话回答的排版', () => {
  it('只认加粗、行内代码、列表、标题；HTML 原样是文字', () => {
    const blocks = parseRichText('**hi** <img src=x onerror=alert(1)>\n`code`\n\n- one\n- two\n1. first\n# Title')
    expect(blocks[0]).toEqual({ kind: 'paragraph', lines: [
      [{ kind: 'strong', text: 'hi' }, { kind: 'text', text: ' <img src=x onerror=alert(1)>' }],
      [{ kind: 'code', text: 'code' }],
    ] })
    expect(blocks[1]).toEqual({ kind: 'list', ordered: false, items: [[{ kind: 'text', text: 'one' }], [{ kind: 'text', text: 'two' }]] })
    expect(blocks[2]).toEqual({ kind: 'list', ordered: true, items: [[{ kind: 'text', text: 'first' }]] })
    expect(blocks[3]).toEqual({ kind: 'heading', content: [{ kind: 'text', text: 'Title' }] })
  })
})

describe('学习模式引用的源码', () => {
  it('每一处引用的文件都还在（第一个从仓库根算，后面的相对 ai-sql-agent/src/ai_sql_agent/）', () => {
    const repo = resolve(process.cwd(), '..')
    const cited = Object.values(LEARN_SOURCES)
    expect(cited.length).toBe(6)
    for (const chunk of cited) {
      chunk.split(' · ').forEach((item, index) => {
        const path = item.trim().replace(/\s*\(.*\)$/, '')
        const full = index === 0 ? resolve(repo, path) : resolve(repo, 'ai-sql-agent/src/ai_sql_agent', path)
        expect(existsSync(full), item).toBe(true)
      })
    }
  })
})
