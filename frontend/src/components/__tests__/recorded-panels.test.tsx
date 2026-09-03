/**
 * 用真后端录下来的 /monitor 与 /search 响应渲染面板。
 * api 层按 EvalPanel.test 的方式 mock，其余（parseSseBlock 等）保留原实现。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fixtureJson } from '@/test/fixtures'
import type { KnowledgeStats, MonitorSummary, SearchResponse } from '@/api/types'

const monitor = fixtureJson<MonitorSummary>('monitor.json')
const search = fixtureJson<SearchResponse>('zh-search.json')
const health = fixtureJson<{ knowledge?: unknown }>('health.json')

const mocks = vi.hoisted(() => ({
  monitor: vi.fn(),
  search: vi.fn(),
  knowledgeStats: vi.fn(),
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: {
    monitor: () => mocks.monitor(),
    search: () => mocks.search(),
    knowledgeStats: () => mocks.knowledgeStats(),
  },
}))

const { MonitorPanel } = await import('../MonitorPanel')
const { KnowledgePanel } = await import('../KnowledgePanel')

const wrap = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {children}
  </QueryClientProvider>
)

describe('录制的 /monitor', () => {
  it('策略校验标的是校验次数，不是回复条数', async () => {
    mocks.monitor.mockResolvedValue(monitor)
    render(<MonitorPanel base="/api" />, { wrapper: wrap })
    expect(await screen.findByText('策略校验')).toBeInTheDocument()
    expect(screen.getByText(String(monitor.routing!.answer_policy!.checked_total))).toBeInTheDocument()
    expect(screen.getByText('校验次数（流式逐句计）')).toBeInTheDocument()
    expect(screen.getByText('拦截数 / 校验次数')).toBeInTheDocument()
    expect(screen.queryByText('出站回复总数')).not.toBeInTheDocument()
  })

  it('服务过请求的实例按录制值渲染', async () => {
    mocks.monitor.mockResolvedValue(monitor)
    render(<MonitorPanel base="/api" />, { wrapper: wrap })
    await screen.findByText('策略校验')
    for (const [key, s] of Object.entries(monitor.agent_stats)) {
      if (!s.total) continue
      const row = screen.getByText(key).closest('.meter-row') as HTMLElement
      expect(within(row).getByText(s.routing_score.toFixed(2))).toBeInTheDocument()
    }
  })

  it('同一 metric 的多条告警日志收敛成一条', async () => {
    // 后端 active_alerts 是追加日志：同一条件每个采集周期复发一条。
    // 录制时阈值已修好、没有告警，这里按真实条目形状追加三条同 metric 的记录。
    const entry = {
      severity: 'warning', metric: 'agent_avg_ms:order_0',
      message: 'order_0 的 agent_avg_ms = 11355.200，阈值 3000',
      value: 11355.2, threshold: 3000, resolved: false,
    }
    mocks.monitor.mockResolvedValue({
      ...monitor,
      active_alerts: ['02:04:00', '02:04:10', '02:04:20'].map((t) => ({ ...entry, ts: `2026-09-02T${t}` })),
    })
    render(<MonitorPanel base="/api" />, { wrapper: wrap })
    const card = (await screen.findByText('活跃告警')).closest('.card') as HTMLElement
    expect(within(card).getByText('1')).toBeInTheDocument()
    expect(within(card).getAllByText(entry.metric)).toHaveLength(1)
  })
})

describe('录制的 /search', () => {
  it('五条命中五篇不同文档，逐条渲染，不出现重复', async () => {
    mocks.knowledgeStats.mockResolvedValue({ total_chunks: 6 } satisfies KnowledgeStats)
    mocks.search.mockResolvedValue(search)
    const user = userEvent.setup()
    render(<KnowledgePanel base="/api" />, { wrapper: wrap })
    await user.click(screen.getByRole('button', { name: '门板缝隙不均能自己调吗' }))
    expect(await screen.findByText(`${search.results.length} 条命中`)).toBeInTheDocument()

    const hits = document.querySelectorAll('.hit')
    expect(hits).toHaveLength(search.results.length)
    const bodies = [...hits].map((h) => h.querySelector('.hit-text')!.textContent)
    expect(new Set(bodies).size).toBe(bodies.length)
    // 夹具本身也得是去重后的形状，否则上面在测假的
    expect(new Set(search.results.map((r) => r.content)).size).toBe(search.results.length)
    void health
  })
})
