/**
 * 刷新之后的历史：审计日志里每条带 turn_id，工作台按它从事件日志取回那一轮的经过
 * （GET /turns/{turn_id}），交给原来的检查器和学习模式渲染，不写第二套显示逻辑。
 * 取不到（超出保留期，或者当时日志没开）时照实说；原文按保留期清空之后，结构照常显示。
 *
 * 夹具由后端录：backend/tests/test_turn_projection.py 里的真处理函数，见
 * fixtures/turns/manifest.json。后端改了投影，那边会红。
 */
import type { ReactElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fixtureJson } from '@/test/fixtures'
import type { ConversationResponse, TicketDetail, TurnProjection } from '@/api/types'

const mocks = vi.hoisted(() => ({ turn: vi.fn(), ticket: vi.fn(), conversation: vi.fn() }))
vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>()
  return {
    ...original,
    api: {
      ...original.api,
      turn: (_base: string, id: string) => mocks.turn(id),
      ticket: (_base: string, id: string) => mocks.ticket(id),
      conversation: (_base: string, id: string) => mocks.conversation(id),
    },
  }
})

const { ApiError } = await import('@/api/client')
const { RoutingInspector } = await import('../RoutingInspector')
const { MessageBubble } = await import('../MessageBubble')
const { TicketsPanel } = await import('../TicketsPanel')
const { restoreMessages } = await import('@/hooks/useChatStream')

const BASE = '/api'
const conversation = fixtureJson<ConversationResponse>('turns/conversation.json')
const answered = fixtureJson<TurnProjection>('turns/turn-answered.json')
const handoff = fixtureJson<TurnProjection>('turns/turn-handoff.json')
const redacted = fixtureJson<TurnProjection>('turns/turn-redacted.json')
const ticket = fixtureJson<TicketDetail>('turns/ticket.json')

const restored = restoreMessages(conversation.messages)
const reply = restored[1]!            // 订单专家作答的那一轮
const handedOff = restored[3]!        // 要求人工、建了工单的那一轮

function withQueries(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  mocks.turn.mockReset()
  mocks.ticket.mockReset()
  mocks.conversation.mockReset()
})

describe('恢复出来的消息带着 turn_id', () => {
  it('每条助手回复都知道自己是哪一轮', () => {
    expect(restored.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant'])
    expect(reply.turnId).toBe(answered.turn_id)
    expect(handedOff.turnId).toBe(handoff.turn_id)
  })

  it('工单按 turn_id 对回那一轮：request_id 对不上也找得到', () => {
    const [, , , withTicket] = restoreMessages(conversation.messages, [{ ...ticket, request_id: 'from-a-client' }])
    expect(withTicket!.ticketId).toBe(ticket.id)
  })
})

describe('编排检查器', () => {
  it('打开刷新前的那一轮时，按 turn_id 取回来，照当轮的样子显示', async () => {
    mocks.turn.mockResolvedValue(answered)
    withQueries(<RoutingInspector message={reply} base={BASE} />)

    expect(await screen.findByText('missing_part')).toBeInTheDocument()
    expect(mocks.turn).toHaveBeenCalledWith(answered.turn_id)
    expect(screen.getByText(answered.routing.reason!)).toBeInTheDocument()   // 路由原文
    expect(screen.queryByText(/没有这一轮的经过记录/)).toBeNull()
  })

  it('超出保留期时说清楚是取不到，不是没有发生', async () => {
    mocks.turn.mockRejectedValue(new ApiError(404, '这一轮的经过不存在或已超出保留期'))
    withQueries(<RoutingInspector message={reply} base={BASE} />)
    expect(await screen.findByText(/超出了保留期/)).toBeInTheDocument()
  })

  it('原文清空之后结构照常显示，并先说一句原文没了', async () => {
    mocks.turn.mockResolvedValue(redacted)
    withQueries(<RoutingInspector message={reply} base={BASE} />)
    expect(await screen.findByText(/原文已按保留期删除/)).toBeInTheDocument()
    expect(screen.getByText('missing_part')).toBeInTheDocument()
  })

  it('worker 中途失联的那一遍：说清楚是第几次执行接的手，不只说"不完整"', async () => {
    mocks.turn.mockResolvedValue({
      ...answered, complete: false, abandoned: { reason: 'superseded', attempt: 1, by_attempt: 2 },
    })
    withQueries(<RoutingInspector message={reply} base={BASE} />)
    expect(await screen.findByText(/第 1 次执行的 worker 在中途失联，由第 2 次执行接了手/)).toBeInTheDocument()
    expect(screen.queryByText(/中间丢过事件/)).toBeNull()
  })

  it('进了死信的那一遍说进了死信；老后端没有 abandoned 时照旧说不完整', async () => {
    mocks.turn.mockResolvedValueOnce({ ...answered, complete: false, abandoned: { reason: 'dead_letter', attempt: 2 } })
    const first = withQueries(<RoutingInspector message={reply} base={BASE} />)
    expect(await screen.findByText(/第 2 次执行的 worker 在中途失联，这个 run 进了死信/)).toBeInTheDocument()
    first.unmount()

    const { abandoned: _dropped, ...older } = answered
    mocks.turn.mockResolvedValueOnce({ ...older, complete: false })
    withQueries(<RoutingInspector message={reply} base={BASE} />)
    expect(await screen.findByText(/中间丢过事件/)).toBeInTheDocument()
  })

  it('没有 turn_id 的老记录照旧说没有，也不去请求', () => {
    // 不包 QueryClientProvider：这条路径不该用到它
    render(<RoutingInspector message={{ ...reply, turnId: undefined }} base={BASE} />)
    expect(screen.getByText(/没有这一轮的经过记录/)).toBeInTheDocument()
    expect(mocks.turn).not.toHaveBeenCalled()
  })
})

describe('学习模式', () => {
  it('刷新前的那一轮点了才取，取回来照当轮讲', async () => {
    mocks.turn.mockResolvedValue(answered)
    const user = userEvent.setup()
    withQueries(<MessageBubble message={reply} explain base={BASE} onInspect={() => {}} />)

    expect(mocks.turn).not.toHaveBeenCalled()             // 历史有几十条时不为每一条都发请求
    await user.click(screen.getByRole('button', { name: /这一轮的经过/ }))
    expect(await screen.findByText('本轮发生了什么')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开编排检查器' })).toBeInTheDocument()
  })

  it('没开学习模式时不出这个按钮', () => {
    withQueries(<MessageBubble message={reply} base={BASE} />)
    expect(screen.queryByRole('button', { name: /这一轮的经过/ })).toBeNull()
  })
})

describe('工单详情', () => {
  it('能打开转人工的那一轮', async () => {
    mocks.ticket.mockResolvedValue(ticket)
    mocks.conversation.mockResolvedValue(conversation)
    mocks.turn.mockResolvedValue(handoff)
    const user = userEvent.setup()
    withQueries(<TicketsPanel base={BASE} ticketId={ticket.id} />)

    await user.click(await screen.findByRole('button', { name: '查看' }))
    expect(mocks.turn).toHaveBeenCalledWith(ticket.turn_id)
    // 意图在下面的"技术详情"里也有一份：只在这一节里找
    const section = screen.getByRole('region', { name: '转人工那一轮的经过' })
    expect(await within(section).findByText('human_handoff')).toBeInTheDocument()
    // 直接转人工的路由原因和转人工原因是同一句：两处都在
    expect(within(section).getAllByText(handoff.result.handoff_reason!).length).toBeGreaterThan(0)
  })

  it('老工单没有 turn_id：这一节不出现', async () => {
    mocks.ticket.mockResolvedValue({ ...ticket, turn_id: '' })
    mocks.conversation.mockResolvedValue(conversation)
    withQueries(<TicketsPanel base={BASE} ticketId={ticket.id} />)
    expect(await screen.findByText(ticket.id)).toBeInTheDocument()
    expect(screen.queryByText('转人工那一轮的经过')).toBeNull()
  })
})
