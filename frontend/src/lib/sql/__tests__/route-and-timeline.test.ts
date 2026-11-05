import { describe, expect, it } from 'vitest'
import { href, parseRoute } from '@/lib/route'
import type { RunEvent } from '@/api/sql/types'
import runEvents from '@/test/fixtures/sql/run-events.json'
import { buildTimeline, rowDuration, rowStatus, runDuration } from '../runTimeline'

describe('数据页面的地址', () => {
  it('四个位置都认，链接和解析互为往返', () => {
    expect(parseRoute(href.dataAsk())).toEqual({ view: 'data', page: 'ask' })
    expect(parseRoute(href.dataHistory())).toEqual({ view: 'data', page: 'history' })
    expect(parseRoute(href.dataExplore())).toEqual({ view: 'data', page: 'explore', query: '' })
    expect(parseRoute(href.dataExplore('erp db', 'a/b', 'q=x&page=2')))
      .toEqual({ view: 'data', page: 'explore', database: 'erp db', table: 'a/b', query: 'q=x&page=2' })
    expect(parseRoute(href.dataRun('abc123'))).toEqual({ view: 'data', page: 'run', runId: 'abc123' })
  })

  it('问号后面那段是表页自己的状态，不参与分段', () => {
    expect(parseRoute('#/data/explore/db/t?f=col%3Aeq%3Aa%2Fb')).toMatchObject({ database: 'db', table: 't', query: 'f=col%3Aeq%3Aa%2Fb' })
    // 表名里真有问号时，链接里它是转义过的
    expect(parseRoute(href.dataExplore('db', 'what?'))).toMatchObject({ table: 'what?', query: '' })
  })

  it('认不出的数据地址落到问数，不报错；别的页面照旧', () => {
    expect(parseRoute('#/data/nope')).toEqual({ view: 'data', page: 'ask' })
    expect(parseRoute('#/data/runs')).toEqual({ view: 'data', page: 'ask' })
    expect(parseRoute('#/data/explore/%E0%A4%A')).toMatchObject({ view: 'data', page: 'explore' })
    expect(parseRoute('#/tickets/tk_1')).toEqual({ view: 'tickets', ticketId: 'tk_1' })
  })
})

describe('一次查询的经过：事件 → 时间线', () => {
  const events = runEvents.events as RunEvent[]

  it('录下来的那次 run：每个阶段一行，SQL 挂在执行那一步下面', () => {
    const timeline = buildTimeline(events)
    expect(timeline.started?.type).toBe('run.started')
    expect(timeline.finished?.type).toBe('run.finished')
    const stages = timeline.rows.map((row) => `${row.stage}${row.step ? `#${row.step}` : ''}`)
    expect(stages).toEqual(events.filter((event) => event.type === 'stage.started')
      .map((event) => `${event.stage}${event.step ? `#${event.step}` : ''}`))
    const execution = timeline.rows.find((row) => row.stage === 'execution')!
    expect(execution.events.map((event) => event.type)).toEqual(['tool.sql'])
    expect(timeline.rows.every((row) => rowStatus(row) === 'ok' && rowDuration(row) !== null)).toBe(true)
  })

  it('事件乱序到达也按 seq 排；结束事件对不上任何开始的单独成行，不丢', () => {
    const shuffled = [...events].reverse()
    expect(buildTimeline(shuffled).rows.map((row) => row.key)).toEqual(buildTimeline(events).rows.map((row) => row.key))

    const orphan: RunEvent = { run_id: 'r', seq: 999, ts: '', type: 'stage.finished', stage: 'repair', step: 2,
      attrs: { status: 'error', error: '列不存在' } }
    const rows = buildTimeline([...events, orphan]).rows
    expect(rows[rows.length - 1]).toMatchObject({ stage: 'repair', step: 2, started: null })
    expect(rowStatus(rows[rows.length - 1]!)).toBe('error')
  })

  it('同一个阶段按步骤重复时各占一行；还没结束的是 running', () => {
    const mk = (seq: number, type: string, stage: string, step: number | null, attrs = {}): RunEvent =>
      ({ run_id: 'r', seq, ts: '', type, stage, step, attrs })
    const rows = buildTimeline([
      mk(1, 'stage.started', 'execution', 1), mk(2, 'tool.sql', 'execution', 1), mk(3, 'stage.finished', 'execution', 1),
      mk(4, 'stage.started', 'execution', 2), mk(5, 'tool.sql', 'execution', 2),
    ]).rows
    expect(rows.map((row) => [row.step, row.events.length, rowStatus(row)])).toEqual([[1, 1, 'ok'], [2, 1, 'running']])
  })

  it('总耗时：结束减开始，任一缺失就不算', () => {
    expect(runDuration('2026-11-05T21:31:21.776Z', '2026-11-05T21:31:21.796Z')).toBe(20)
    expect(runDuration('2026-11-05T21:31:21.776Z', null)).toBeNull()
  })
})
