import { describe, expect, it } from 'vitest'
import type { HistoryResponse, QueryResponse } from '../generated'
import type {
  Catalog, CatalogTable, FeedbackStats, Readiness, RowsPage, RunEventsPage, RunRecord, SchemaStatus, TableProfile, WhoAmI,
} from '../types'
import catalog from '@/test/fixtures/sql/catalog.json'
import feedbackStats from '@/test/fixtures/sql/feedback-stats.json'
import history from '@/test/fixtures/sql/history.json'
import profile from '@/test/fixtures/sql/profile.json'
import query from '@/test/fixtures/sql/query.json'
import readyz from '@/test/fixtures/sql/readyz.json'
import rows from '@/test/fixtures/sql/rows.json'
import rowsFiltered from '@/test/fixtures/sql/rows-filtered.json'
import run from '@/test/fixtures/sql/run.json'
import runEvents from '@/test/fixtures/sql/run-events.json'
import schemaStatus from '@/test/fixtures/sql/schema-status.json'
import table from '@/test/fixtures/sql/table.json'
import whoami from '@/test/fixtures/sql/whoami.json'

/**
 * 手写的类型（types.ts）对得上 SQL Agent 真实的响应。
 *
 * 夹具由 ai-sql-agent/scripts/record_console_fixtures.py 在进程里录：演示库、Mock 模型，全程离线。
 * 下面每一行赋值都由类型检查把关（tsc -b 也检查测试文件）：服务端改了格式、夹具一重录，这里就编译不过。
 * 运行时再核几样关系，确认夹具本身是一次完整的、彼此对得上的录制。
 */
const typed = {
  catalog: catalog satisfies Catalog,
  table: table satisfies CatalogTable,
  rows: rows satisfies RowsPage,
  rowsFiltered: rowsFiltered satisfies RowsPage,
  profile: profile satisfies TableProfile,
  whoami: whoami satisfies WhoAmI,
  run: run satisfies RunRecord,
  runEvents: runEvents satisfies RunEventsPage,
  history: history satisfies HistoryResponse,
  query: query.response satisfies QueryResponse,
  feedbackStats: feedbackStats satisfies FeedbackStats,
  schemaStatus: schemaStatus satisfies SchemaStatus,
  readyz: readyz satisfies Readiness,
}

describe('录下来的 SQL Agent 响应', () => {
  it('历史里的 run_id 在运行记录里找得到，/query 的 X-Run-Id 就是那次 run', () => {
    expect(typed.run.run_id).toBe(query.run_id)
    expect(typed.history.items?.map((item) => item.run_id)).toContain(query.run_id)
    expect(typed.runEvents.run_id).toBe(query.run_id)
  })

  it('事件日志从 run.started 开始、到 run.finished 结束，序号连续', () => {
    const events = typed.runEvents.events
    expect(events[0]!.type).toBe('run.started')
    expect(events[events.length - 1]!.type).toBe('run.finished')
    expect(events.map((event) => event.seq)).toEqual(events.map((_, index) => index + 1))
    expect(events.some((event) => event.type === 'tool.sql')).toBe(true)
  })

  it('录制用的是控制台那把管理员 Key 的身份', () => {
    expect(typed.whoami.identity).toMatchObject({ caller: 'console', is_admin: true })
    expect(typed.catalog.identity).toEqual(typed.whoami.identity)
  })
})
