import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { EvalReport } from '@/api/types'

const runEval = vi.fn()
vi.mock('@/api/client', () => ({ api: { runEval: () => runEval() } }))

const { EvalPanel } = await import('../EvalPanel')

const REPORT: EvalReport = {
  release: 'r1', timestamp: '2026-09-01T00:00:00',
  pass_rate: 1, total: 1, passed: 1,
  avg_scores: { relevance: 0.9, accuracy: 0.9, completeness: 0.9, helpfulness: 0.9 },
  regressions: [], recommendations: [], promoted: false,
  baseline: { release: 'r0', timestamp: '2026-08-01T00:00:00' },
  results: [{ test_id: 'dialog_0', passed: true, scores: { relevance: 0.9 }, detail: 'ok', metadata: {} }],
}

/** 一个手动控制何时 resolve 的 /eval/run——评测是分钟级的，测的就是这段等待。 */
function deferred() {
  let resolve!: (v: EvalReport) => void
  const promise = new Promise<EvalReport>((r) => { resolve = r })
  return { promise, resolve }
}

const wrap = (client: QueryClient) => ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
)

describe('EvalPanel', () => {
  beforeEach(() => runEval.mockReset())

  it('切走再切回来，正在跑的评测不会丢', async () => {
    const d = deferred()
    runEval.mockReturnValueOnce(d.promise)
    const client = new QueryClient()
    const user = userEvent.setup()

    const first = render(<EvalPanel base="/api" />, { wrapper: wrap(client) })
    await user.click(screen.getByRole('button', { name: '运行评测' }))
    expect(await screen.findByText(/正在跑用例/)).toBeInTheDocument()

    // 切到别的页面：App 里是 {view === 'eval' && <EvalPanel/>}，面板会真的卸载
    first.unmount()
    const second = render(<EvalPanel base="/api" />, { wrapper: wrap(client) })
    expect(await screen.findByText(/正在跑用例/)).toBeInTheDocument()

    // 结果在面板重新挂载后落地，照样要渲染出来
    d.resolve(REPORT)
    expect(await screen.findByText(/通过率/)).toBeInTheDocument()
    expect(runEval).toHaveBeenCalledTimes(1)
    second.unmount()
  })

  it('跑完切走再回来，结果还在，不用重跑', async () => {
    runEval.mockResolvedValueOnce(REPORT)
    const client = new QueryClient()
    const user = userEvent.setup()

    const first = render(<EvalPanel base="/api" />, { wrapper: wrap(client) })
    await user.click(screen.getByRole('button', { name: '运行评测' }))
    expect(await screen.findByText(/通过率/)).toBeInTheDocument()

    first.unmount()
    render(<EvalPanel base="/api" />, { wrapper: wrap(client) })
    expect(await screen.findByText(/通过率/)).toBeInTheDocument()
    expect(runEval).toHaveBeenCalledTimes(1)
  })

  it('评测跑着的时候不接受第二次触发，避免并发烧两轮 LLM', async () => {
    const d = deferred()
    runEval.mockReturnValue(d.promise)
    const client = new QueryClient()
    const user = userEvent.setup()

    render(<EvalPanel base="/api" />, { wrapper: wrap(client) })
    await user.click(screen.getByRole('button', { name: '运行评测' }))
    const button = await screen.findByRole('button', { name: '评测中…' })
    expect(button).toBeDisabled()

    d.resolve(REPORT)
    await waitFor(() => expect(runEval).toHaveBeenCalledTimes(1))
  })
})
