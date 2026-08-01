/**
 * 主导航只有三项：对话、知识库、工单。调试和配置类的入口都收在「管理与诊断」后面。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@/test/render'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const health = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: { health: () => health() },
}))

const { Sidebar } = await import('../Sidebar')

function show(view: 'chat' | 'knowledge' | 'tickets' | 'admin' = 'chat') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Sidebar base="/api" view={view} onNewChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    { lang: 'en', wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> },
  )
}

describe('侧栏导航', () => {
  // 花括号不能省：mockReset() 返回 mock 本身，而 beforeEach 返回的函数会被 vitest
  // 当成清理函数在用例结束后调用——等于每个用例收尾时多调一次 health()。
  beforeEach(() => { health.mockReset() })

  it('主导航是对话、知识库、工单三项，管理与诊断单独在下面', async () => {
    health.mockResolvedValue({ status: 'ok', agents: {}, tickets: { open: 2, total: 5 } })
    show('knowledge')

    const primary = screen.getByRole('navigation', { name: 'Workspace' })
    expect(within(primary).getAllByRole('link').map((a) => a.getAttribute('href')))
      .toEqual(['#/chat', '#/knowledge', '#/tickets'])
    expect(within(primary).getByRole('link', { name: 'Knowledge' })).toHaveAttribute('aria-current', 'page')

    const admin = screen.getByRole('navigation', { name: 'Administration' })
    expect(within(admin).getByRole('link', { name: /Admin & diagnostics/ })).toHaveAttribute('href', '#/admin')

    // 监控、评测、检查器、模型、API 配置都不在第一屏的导航里
    for (const name of [/monitor/i, /eval/i, /inspector/i, /model/i, /api/i, /settings/i]) {
      expect(screen.queryByRole('link', { name })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()   // 不用先填 user_id / 会话 id / API 地址

    // 待处理工单数来自 /health，不是写死的
    expect(await within(primary).findByText('2')).toBeInTheDocument()
    expect(await screen.findByText('Online')).toBeInTheDocument()
  })

  it('后端没报工单统计时不显示数字；连不上时照实显示', async () => {
    health.mockRejectedValue(new TypeError('Failed to fetch'))
    show()
    expect(await screen.findByText('Unreachable')).toBeInTheDocument()
    expect(document.querySelector('.nav-item .count')).toBeNull()
  })
})
