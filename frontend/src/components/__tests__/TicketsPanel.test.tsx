/**
 * 工单页：列表、筛选、详情、状态流转。
 * 界面只显示后端有的东西——没有负责人、没有时限，通知只转述投递记录。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { dateTime } from '@/lib/format'
import type { ConversationResponse, TicketDetail, TicketList } from '@/api/types'

const mocks = vi.hoisted(() => ({
  tickets: vi.fn(),
  ticket: vi.fn(),
  setTicketStatus: vi.fn(),
  conversation: vi.fn(),
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: {
    tickets: (_base: string, status: string) => mocks.tickets(status),
    ticket: (_base: string, id: string) => mocks.ticket(id),
    setTicketStatus: (_base: string, id: string, status: string) => mocks.setTicketStatus(id, status),
    conversation: (_base: string, id: string) => mocks.conversation(id),
  },
}))

const { ApiError } = await import('@/api/client')
const { TicketsPanel } = await import('../TicketsPanel')

const OPEN: TicketDetail = {
  id: 'tk_24032b0898d5', conv_id: 'conv-1', user_id: 'lab-verification', request_id: 'req-9',
  reason: '意图为 human_handoff，用户明确要求人工', intent: 'human_handoff', urgency: '',
  answer_confidence: 0.4, last_message: 'My countertop has a structural crack. I want a human agent.',
  status: 'open', created_at: 1_759_617_430, updated_at: 1_759_617_430,
  allowed_transitions: ['assigned', 'resolved', 'cancelled'],
  notification: null, notifications_enabled: false,
}

const LIST: TicketList = {
  notifications_enabled: false,
  stats: { open: 1, assigned: 1, resolved: 0, cancelled: 0, total: 2 },
  tickets: [
    OPEN,
    { ...OPEN, id: 'tk_baaa7a5ebfd1', status: 'assigned', last_message: 'The ship-to address on my invoice is wrong.' },
  ],
}

const CONVERSATION: ConversationResponse = {
  conv_id: 'conv-1',
  messages: [
    { seq: 2, conv_id: 'conv-1', user_id: 'u', request_id: 'req-9', role: 'assistant',
      content: 'This needs to be handled by a member of our team.', agent_type: 'handoff', created_at: 1_759_617_431 },
    { seq: 1, conv_id: 'conv-1', user_id: 'u', request_id: 'req-9', role: 'user',
      content: 'My countertop has a structural crack. I want a human agent.', agent_type: '', created_at: 1_759_617_430 },
  ],
}

function show(ticketId?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<TicketsPanel base="/api" ticketId={ticketId} />, {
    lang: 'en',
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  })
}

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset())
  mocks.tickets.mockResolvedValue(LIST)
  mocks.conversation.mockResolvedValue(CONVERSATION)
})

describe('工单列表', () => {
  it('显示编号、问题摘要、状态和创建时间，编号能点进详情', async () => {
    show()
    const link = await screen.findByRole('link', { name: 'tk_24032b0898d5' })
    expect(link).toHaveAttribute('href', '#/tickets/tk_24032b0898d5')

    const row = link.closest('tr')!
    expect(within(row).getByText(/structural crack/)).toBeInTheDocument()
    expect(within(row).getByText('Open')).toBeInTheDocument()
    // 后端的 created_at 原样换算成本地时间，不是"刚刚"这类说不清的相对时间
    expect(within(row).getByText(dateTime(OPEN.created_at, 'en'))).toBeInTheDocument()

    expect(screen.getByRole('columnheader', { name: 'Created' })).toBeInTheDocument()
    // 系统没有的字段，界面上也没有
    expect(screen.queryByRole('columnheader', { name: /assign|owner|sla|due/i })).not.toBeInTheDocument()
  })

  it('没配通知渠道时在页面上说明：新工单不会自动提醒任何人', async () => {
    show()
    expect(await screen.findByText(/nobody is alerted automatically/)).toBeInTheDocument()
  })

  it('按状态筛选是向后端要那个状态的工单', async () => {
    show()
    await screen.findByRole('link', { name: 'tk_24032b0898d5' })
    expect(screen.getByRole('tab', { name: /^Open/ })).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: /^All/ })).toHaveTextContent('2')

    mocks.tickets.mockResolvedValue({ ...LIST, tickets: [] })
    await userEvent.click(screen.getByRole('tab', { name: /^Resolved/ }))
    expect(mocks.tickets).toHaveBeenLastCalledWith('resolved')
    expect(await screen.findByText('No tickets with status “Resolved”')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /^Resolved/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('接口一次给不全时说明只显示了多少，不让人以为就这些', async () => {
    mocks.tickets.mockResolvedValue({ ...LIST, stats: { open: 240, assigned: 1, total: 241 } })
    show()
    expect(await screen.findByText(/Showing the newest 2 of 241/)).toBeInTheDocument()
  })

  it('一张工单都没有和接口出错是两回事', async () => {
    mocks.tickets.mockResolvedValue({ ...LIST, tickets: [], stats: { total: 0 } })
    const empty = show()
    expect(await screen.findByText('No tickets yet')).toBeInTheDocument()
    empty.unmount()

    mocks.tickets.mockRejectedValue(new ApiError(401, '401 Unauthorized: 缺少或错误的 X-API-Key'))
    show()
    expect(await screen.findByText('Tickets could not be loaded')).toBeInTheDocument()
    expect(screen.getByText(/requires a staff access key/)).toBeInTheDocument()
    expect(screen.queryByText('No tickets yet')).not.toBeInTheDocument()
  })
})

describe('工单详情', () => {
  it('显示问题、转人工原因、关联对话；通知只说投递记录里有的', async () => {
    mocks.ticket.mockResolvedValue(OPEN)
    show(OPEN.id)

    expect(await screen.findByRole('heading', { name: /tk_24032b0898d5/ })).toBeInTheDocument()
    expect(screen.getByText('Customer request').closest('section'))
      .toHaveTextContent('My countertop has a structural crack')
    // 英文界面把系统记的原因对译出来；原文留在技术详情里，能和后端日志对上
    expect(screen.getByText('Why it was handed off').closest('section'))
      .toHaveTextContent('The customer explicitly asked for a person (intent: human_handoff).')
    expect(screen.getByText('Reason as recorded').closest('div'))
      .toHaveTextContent('意图为 human_handoff，用户明确要求人工')
    expect(screen.getByText(/No notification channel is configured, so nobody was alerted/)).toBeInTheDocument()
    expect(screen.getByText(/does not track who a ticket is assigned to, or a response deadline/)).toBeInTheDocument()

    // 关联对话按 seq 排回时间顺序
    const transcript = (await screen.findByText('Customer question')).closest('ol')!
    const turns = within(transcript).getAllByRole('listitem')
    expect(turns[0]).toHaveTextContent('I want a human agent')
    expect(turns[0]).toHaveTextContent('created this ticket')
    expect(turns[1]).toHaveTextContent('This needs to be handled by a member of our team.')
    expect(mocks.conversation).toHaveBeenCalledWith('conv-1')
  })

  it.each([
    [{ status: 'pending', attempts: 2, last_error: 'HTTP 503', updated_at: 1 }, /Queued, not yet delivered \(2 attempts so far\)/],
    [{ status: 'delivered', attempts: 1, last_error: '', updated_at: 1_759_617_500 }, /accepted, not that someone has read it/],
    [{ status: 'failed', attempts: 5, last_error: 'HTTP 404: gone', updated_at: 1 }, /Delivery failed after 5 attempts\. Nobody was alerted/],
  ])('通知状态 %o 照投递记录说', async (notification, expected) => {
    mocks.ticket.mockResolvedValue({ ...OPEN, notification, notifications_enabled: true })
    show(OPEN.id)
    expect(await screen.findByText(expected)).toBeInTheDocument()
    expect(screen.queryByText(/someone (has )?(received|picked)|call (you )?back/i)).not.toBeInTheDocument()
  })

  it('状态流转走接口；成功后显示后端返回的新状态', async () => {
    mocks.ticket.mockResolvedValue(OPEN)
    mocks.setTicketStatus.mockResolvedValue({
      ...OPEN, status: 'assigned', allowed_transitions: ['open', 'resolved', 'cancelled'],
    })
    show(OPEN.id)
    await userEvent.click(await screen.findByRole('button', { name: 'Mark as assigned' }))

    expect(mocks.setTicketStatus).toHaveBeenCalledWith(OPEN.id, 'assigned')
    expect(await screen.findByRole('button', { name: 'Return to open' })).toBeInTheDocument()
    expect(document.querySelector('.reader-head .chip')).toHaveTextContent('Assigned')
  })

  it('流转失败时保留原状态并说明原因', async () => {
    mocks.ticket.mockResolvedValue(OPEN)
    mocks.setTicketStatus.mockRejectedValue(
      new ApiError(422, '422 Unprocessable Entity: 工单 tk_24032b0898d5 不允许从 resolved 流转到 assigned'),
    )
    show(OPEN.id)
    await userEvent.click(await screen.findByRole('button', { name: 'Mark as assigned' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The status was not changed.')
    expect(alert).toHaveTextContent('does not allow this change')
    expect(alert).toHaveTextContent('不允许从 resolved 流转到 assigned')   // 后端的原话留着
    expect(document.querySelector('.reader-head .chip')).toHaveTextContent('Open')
    expect(screen.getByRole('button', { name: 'Mark as assigned' })).toBeEnabled()
  })

  it('进入终态前要确认一次，可以反悔', async () => {
    mocks.ticket.mockResolvedValue(OPEN)
    show(OPEN.id)
    await userEvent.click(await screen.findByRole('button', { name: 'Mark as resolved' }))
    expect(screen.getByText('A ticket marked “Resolved” cannot be reopened.')).toBeInTheDocument()
    expect(mocks.setTicketStatus).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Keep as is' }))
    expect(mocks.setTicketStatus).not.toHaveBeenCalled()

    mocks.setTicketStatus.mockResolvedValue({ ...OPEN, status: 'resolved', allowed_transitions: [] })
    await userEvent.click(screen.getByRole('button', { name: 'Mark as resolved' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(mocks.setTicketStatus).toHaveBeenCalledWith(OPEN.id, 'resolved')
    expect(await screen.findByText(/This ticket is closed/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Mark as|Cancel ticket|Return to open/ })).not.toBeInTheDocument()
  })

  it('中文界面原样显示系统记的原因，不另列一份原文', async () => {
    mocks.ticket.mockResolvedValue({ ...OPEN, reason: 'Agent 自评置信度 0.65 低于 0.7' })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<TicketsPanel base="/api" ticketId={OPEN.id} />, {
      lang: 'zh',
      wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    })
    expect((await screen.findByText('转人工原因')).closest('section')).toHaveTextContent('Agent 自评置信度 0.65 低于 0.7')
    expect(screen.queryByText('系统记录的原因原文')).not.toBeInTheDocument()
  })

  /**
   * 只用键盘走完"标记为已解决"。实测撞到的：按下回车后原来的按钮被确认提示换掉，
   * 焦点掉回 <body>——看不到焦点在哪，Esc 也不管用。
   */
  it('键盘操作：确认提示接住焦点，Esc 反悔后焦点回到原按钮，提交后落在状态标题上', async () => {
    mocks.ticket.mockResolvedValue(OPEN)
    show(OPEN.id)
    const user = userEvent.setup()

    const resolve = await screen.findByRole('button', { name: 'Mark as resolved' })
    resolve.focus()
    await user.keyboard('{Enter}')
    // 不可撤销的操作：焦点默认停在"保持不变"上，连按两下回车不会误关工单
    const keep = screen.getByRole('button', { name: 'Keep as is' })
    expect(keep).toHaveFocus()
    expect(screen.getByRole('group', { name: 'Mark as resolved' }))
      .toHaveAccessibleDescription('A ticket marked “Resolved” cannot be reopened.')

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('button', { name: 'Keep as is' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mark as resolved' })).toHaveFocus()
    expect(mocks.setTicketStatus).not.toHaveBeenCalled()

    mocks.setTicketStatus.mockResolvedValue({ ...OPEN, status: 'resolved', allowed_transitions: [] })
    await user.keyboard('{Enter}')
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(screen.getByRole('button', { name: 'Confirm' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(mocks.setTicketStatus).toHaveBeenCalledWith(OPEN.id, 'resolved')
    expect(screen.getByRole('heading', { name: 'Status' })).toHaveFocus()
    expect(await screen.findByText(/This ticket is closed/)).toBeInTheDocument()
  })

  it('工单不存在、对话已清理，各自说清楚', async () => {
    mocks.ticket.mockRejectedValue(new ApiError(404, '404 Not Found: 工单不存在: tk_x'))
    const missing = show('tk_x')
    expect(await screen.findByText('This ticket does not exist.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'All tickets' })).toHaveAttribute('href', '#/tickets')
    missing.unmount()

    mocks.ticket.mockResolvedValue(OPEN)
    mocks.conversation.mockRejectedValue(new ApiError(404, '404 Not Found: 对话不存在或已超出留存期'))
    show(OPEN.id)
    expect(await screen.findByText('The conversation is no longer stored.')).toBeInTheDocument()
    expect(screen.getByText('Customer request')).toBeInTheDocument()   // 工单本身照常显示
  })

  it('老后端不报可流转状态时不出按钮，也不猜', async () => {
    const { allowed_transitions: _a, notification: _n, notifications_enabled: _e, ...legacy } = OPEN
    mocks.ticket.mockResolvedValue(legacy)
    show(OPEN.id)
    expect(await screen.findByText(/does not report which status changes are allowed/)).toBeInTheDocument()
    expect(screen.getByText(/does not report notification status/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark as assigned' })).not.toBeInTheDocument()
  })
})
