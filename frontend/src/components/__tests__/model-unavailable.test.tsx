/**
 * 「模型服务不可用」和「业务需要转人工」是两件事，界面上不能长成一个样子。
 *
 * 转人工：判断过了，这件事需要人来处理——有工单，有"需要人工跟进"的卡片。
 * 不可用：没能判断——没有回答，没有工单，也不该让员工以为有人在跟。
 * 停机的那一晚每条没答上来的提问都被显示成转人工，这一组盯的就是这个区别。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  applyChatResponse, applyStreamEvent, restoreMessages, type ChatMessage,
} from '@/hooks/useChatStream'
import { parseSseBlock } from '@/api/client'
import { llmReasonKey, llmRecovery } from '@/lib/llm'
import { fixture, fixtureJson, foldStream } from '@/test/fixtures'
import type {
  ChatResponse, ConversationResponse, EvalReport, HealthResponse, StreamEvent, Ticket,
} from '@/api/types'

const health = vi.hoisted(() => vi.fn())
const runEval = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: { health: () => health(), runEval: () => runEval(), sops: () => new Promise(() => {}) },
}))

const { MessageBubble } = await import('../MessageBubble')
const { RoutingInspector } = await import('../RoutingInspector')
const { AdminPanel } = await import('../AdminPanel')
const { EvalPanel } = await import('../EvalPanel')

/**
 * 夹具是后端在这个场景下**实际发出去的东西**：由 backend/tests/test_model_outage.py
 * 里的真处理函数生成（模型那头接的是假中继，全程离线），后端改了形状那边会红。
 * 录的是同一个会话里的三轮：中文提问（凭证被拒，保护触发）→ 英文提问、关闭流式
 * （保护已经在挡）→ "转人工"（规则通道，照常建单）。见 fixtures/outage/manifest.json。
 */
const SSE = 'outage/zh-chat-stream-unavailable.sse'
const EVENTS = fixture(SSE).split('\n\n').map(parseSseBlock).filter((e): e is StreamEvent => !!e)
const RESULT = EVENTS.find((e) => e.event === 'result')!.data as ChatResponse
/** 后端给客户的那句原话 */
const NOTICE = RESULT.response

const streamed = (events: StreamEvent[] = EVENTS): ChatMessage =>
  events.reduce(applyStreamEvent, { id: 'a1', role: 'assistant', content: '', streaming: false } as ChatMessage)

const unavailable = (over: Partial<ChatResponse> = {}): ChatMessage => {
  const message = streamed()
  return { ...message, result: { ...message.result, ...over } }
}

const HANDOFF_WORDS = /Needs human follow-up|Ticket created|View ticket|需要人工跟进|已创建工单|查看工单/

describe('对话里的一轮', () => {
  it('夹具长成真后端的样子：没答、没工单、原因、整段准备被跳过', () => {
    expect(EVENTS.map((e) => e.event)).toEqual(['progress', 'meta', 'routing', 'delta', 'result'])
    expect(RESULT).toMatchObject({
      service_unavailable: true, unavailable_reason: 'credential_rejected',
      handoff: false, ticket_id: null, agent_type: 'system', primary_agent: 'system',
    })
    expect(NOTICE).toContain('没有为它创建工单')
    expect(foldStream(SSE).meta).toMatchObject({
      knowledge: { attempted: false }, business_data: { outcome: 'model_unavailable' },
    })
  })

  it('没作答就说没作答：不署某个 Agent 的名，不出工单卡，写明没有建工单', () => {
    const { container } = render(<MessageBubble message={streamed()} />, { lang: 'en' })

    expect(screen.getByText('System notice')).toBeInTheDocument()
    expect(screen.getByText('Not answered')).toBeInTheDocument()
    expect(screen.getByText(NOTICE)).toBeInTheDocument()            // 客户端看到的那句原话

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('The model service is unavailable.')
    expect(alert).toHaveTextContent('was not answered and no ticket was created')
    expect(alert).toHaveTextContent('not a decision that the case needs a person')
    expect(alert).toHaveTextContent('Cause: the model provider rejected the API key (invalid or disabled).')
    expect(within(alert).getByRole('link', { name: 'View model service status' }))
      .toHaveAttribute('href', '#/admin')

    expect(container.querySelector('.ticket-note')).toBeNull()
    expect(container.textContent).not.toMatch(HANDOFF_WORDS)
    expect(screen.queryByText(/Agent$/)).not.toBeInTheDocument()   // 没有哪个专家"答了"这一条
    expect(screen.queryByText('Working on it…')).not.toBeInTheDocument()
  })

  it('中文界面同样说清这两件事', () => {
    render(<MessageBubble message={streamed()} />)
    expect(screen.getByText('系统提示')).toBeInTheDocument()
    expect(screen.getByText('未作答')).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('模型服务不可用。')
    expect(alert).toHaveTextContent('没有得到回答，系统也没有为它建工单')
    expect(alert).toHaveTextContent('不是判断出这件事需要人工处理')
    expect(alert).toHaveTextContent('客户确实需要人工时，发送“转人工”或“talk to a person”，会照常建单')
  })

  it('关闭流式时一样（录下来的 /chat 响应，英文提问）', () => {
    const response = fixtureJson<ChatResponse>('outage/en-chat-unavailable.json')
    const message = applyChatResponse({ id: 'a', role: 'assistant', content: '' }, response)

    const { container } = render(<MessageBubble message={message} routingConfig={{ fallback_agent: 'general' } as never} />, { lang: 'en' })
    expect(screen.getByText('System notice')).toBeInTheDocument()
    expect(screen.getByText(/the assistant can't answer right now/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('no ticket was created')
    expect(container.querySelector('.ticket-note')).toBeNull()
    expect(screen.queryByText('Working on it…')).not.toBeInTheDocument()
  })

  it('专家被选中、调过、没答上来：也不把这句提示挂在它名下，更不是"兜底替答"', () => {
    const message = applyChatResponse({ id: 'a', role: 'assistant', content: '' }, {
      ...fixtureJson<ChatResponse>('outage/en-chat-unavailable.json'),
      primary_agent: 'technical', routing_confidence: 0.9, unavailable_reason: 'model_error',
    })
    expect(message.routing?.primary_agent).toBe('technical')        // 路由选过它，但它没作答

    const { container } = render(<MessageBubble message={message} routingConfig={{ fallback_agent: 'general' } as never} />, { lang: 'en' })
    expect(screen.getByText('System notice')).toBeInTheDocument()
    expect(screen.queryByText('Product & Install Agent')).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/did not finish this reply/)
  })

  it('流里没有 routing 事件也不会一直停在"正在处理"', () => {
    render(<MessageBubble message={streamed(EVENTS.filter((e) => e.event !== 'routing'))} />, { lang: 'en' })
    expect(screen.getByRole('alert')).toHaveTextContent('The model service is unavailable.')
    expect(screen.queryByText('Understanding the question')).not.toBeInTheDocument()
  })

  it('半截回答之后才失败：流出去的草稿被那句提示替掉，不留一段没写完的话', () => {
    // 这一种录不出来（要一个吐到一半断掉的模型流），照后端 run_stream 的写法手拼
    const events = [
      EVENTS.find((e) => e.event === 'meta')!,
      { event: 'routing', data: { primary_agent: 'technical', supporting_agents: [], reason: 'r', confidence: 0.9 } },
      { event: 'delta', data: { text: '先检查铰链的' } },
      { event: 'delta', data: { text: `\n\n${NOTICE}` } },
      { event: 'result', data: { ...RESULT, primary_agent: 'technical', unavailable_reason: 'model_error' } },
    ] as StreamEvent[]
    render(<MessageBubble message={streamed(events)} />, { lang: 'en' })
    expect(screen.queryByText(/先检查铰链的/)).not.toBeInTheDocument()
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Cause: the model call failed or timed out.')
    expect(screen.getByRole('alert')).toHaveTextContent('Send the question again.')
  })

  it.each([
    ['credential_rejected', /rejected the API key/, /stays unavailable until the key or quota is fixed/],
    ['permission_denied', /does not allow this key or account/, /stays unavailable until/],
    ['quota_exhausted', /out of quota or credit/, /stays unavailable until/],
    ['rate_limited', /rate limiting requests/, /clears by itself after a short wait/],
    ['forbidden', /refused the request without giving a reason/, /clears by itself after a short wait/],
    ['model_error', /failed or timed out/, /Send the question again/],
    ['something_new', /Cause: not reported/, /Send the question again/],   // 认不出的原因不猜
    ['', /Cause: not reported/, /Send the question again/],
  ])('原因 %s：说清是什么、接下来会怎样', (reason, cause, next) => {
    render(<MessageBubble message={unavailable({ unavailable_reason: reason as never })} />, { lang: 'en' })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(cause)
    expect(alert).toHaveTextContent(next)
  })

  it('停机期间客户明确要人工：照常是转人工，有工单卡，不显示成"不可用"', () => {
    const message = foldStream('outage/zh-chat-stream-handoff.sse')
    expect(message.result).toMatchObject({ handoff: true, service_unavailable: false, ticket_status: 'open' })

    render(<MessageBubble message={message} explain />, { lang: 'en' })
    const card = screen.getByText('Needs human follow-up').closest('.ticket-note') as HTMLElement
    expect(within(card).getByRole('link', { name: 'View ticket' }))
      .toHaveAttribute('href', `#/tickets/${message.result!.ticket_id}`)
    expect(screen.getByText('Human handoff')).toBeInTheDocument()
    expect(screen.queryByText('Not answered')).not.toBeInTheDocument()
    expect(screen.queryByText(/model service is unavailable\./i)).not.toBeInTheDocument()
    // 讲解里照实说：是按规则转的人工；这一轮没去查订单是因为模型不可用
    const row = (label: string) => screen.getByText(label, { selector: '.explain-row > b' }).closest('li')!
    expect(row('Handoff')).toHaveTextContent(/A handoff was decided.*Ticket created:/)
    expect(row('Order data')).toHaveTextContent('Not delegated. The model service was unavailable')
  })

  it('老后端没有这两个字段：行为和以前一样', () => {
    const message: ChatMessage = {
      id: 'a3', role: 'assistant', content: 'Adjust the two hinge screws.',
      routing: { primary_agent: 'technical', supporting_agents: [], reason: 'r', confidence: 0.9 },
      result: { answer_confidence: 0.9, policy_violations: [] },
    }
    render(<MessageBubble message={message} />, { lang: 'en' })
    expect(screen.getByText('Product & Install Agent')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('刷新后恢复的历史', () => {
  it('系统提示还是系统提示，标着没作答；转人工那条照常带工单', () => {
    const log = fixtureJson<ConversationResponse>('outage/conversation.json')
    const { tickets } = fixtureJson<{ tickets: Ticket[] }>('outage/tickets.json')
    expect(log.messages.map((m) => m.agent_type)).toEqual(['', 'system', '', 'system', '', 'handoff'])
    expect(tickets).toHaveLength(1)                       // 三轮里只有明确要人工的那一轮建了单

    const restored = restoreMessages(log.messages, tickets)
    expect(restored.map((m) => m.ticketId)).toEqual([undefined, undefined, undefined, undefined, undefined, tickets[0]!.id])

    const notice = render(<MessageBubble message={restored[1]!} />, { lang: 'en' })
    expect(screen.getByText('System notice')).toBeInTheDocument()
    expect(screen.getByText('Not answered')).toBeInTheDocument()
    expect(notice.container.textContent).not.toMatch(HANDOFF_WORDS)
    notice.unmount()

    render(<MessageBubble message={restored[5]!} />, { lang: 'en' })
    expect(screen.getByText('Human handoff')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', `#/tickets/${tickets[0]!.id}`)
    expect(screen.queryByText('Not answered')).not.toBeInTheDocument()
  })
})

describe('学习模式', () => {
  const row = (label: string) => screen.getByText(label, { selector: '.explain-row > b' }).closest('li')!

  it('按没作答来讲：没检索、没委派、没有可检查的回答、没有转人工', () => {
    render(<MessageBubble explain message={streamed()} />, { lang: 'en' })
    expect(row('Knowledge')).toHaveTextContent('Not searched. The model service was already unavailable')
    expect(row('Specialist')).toHaveTextContent(
      'No agent answered. The model service was unavailable: the model provider rejected the API key')
    expect(row('Order data')).toHaveTextContent('Not delegated. The model service was unavailable')
    expect(row('Answer check')).toHaveTextContent('There was no answer to check.')
    expect(row('Handoff')).toHaveTextContent('Not handed off, and no ticket was created.')
    expect(row('Handoff')).toHaveTextContent('still handed off by rule, without the model')
    // 正常轮次的那几句在这里读起来像"一切正常"，不能出现
    expect(document.querySelector('.explain')!.textContent).not.toMatch(/Nothing was removed|Not needed\./)
  })

  it('路由选中过专家就照实说选中过，但没有谁作答', () => {
    const message = streamed()
    render(<MessageBubble explain message={{
      ...message, routing: { ...message.routing!, primary_agent: 'technical' },
    }} />, { lang: 'en' })
    expect(row('Specialist')).toHaveTextContent('No agent answered.')
    expect(row('Specialist')).toHaveTextContent('Product & Install Agent had been selected.')
  })
})

describe('编排检查器', () => {
  it('处置里标出没作答和原因，不标成转人工', () => {
    render(<RoutingInspector message={streamed()} />, { lang: 'en' })
    expect(screen.getByText('Not answered')).toBeInTheDocument()
    expect(screen.getByText(/Cause: the model provider rejected the API key/)).toBeInTheDocument()
    expect(screen.queryByText('Needs human follow-up')).not.toBeInTheDocument()
  })
})

describe('原因到文案', () => {
  it('后端的每一种原因都有说法；恢复方式和后端的处理对得上', () => {
    const reasons = ['credential_rejected', 'permission_denied', 'quota_exhausted', 'rate_limited', 'forbidden', 'model_error']
    expect(new Set(reasons.map(llmReasonKey)).size).toBe(reasons.length)
    expect(reasons.map(llmRecovery)).toEqual(['reset', 'reset', 'reset', 'wait', 'wait', 'retry'])
    expect([llmReasonKey(undefined), llmReasonKey(null), llmReasonKey('new_reason')])
      .toEqual(['llm.reason.unknown', 'llm.reason.unknown', 'llm.reason.unknown'])
  })
})

// ── 管理与诊断 ──────────────────────────────────────────────────────────────

const wrap = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

const HEALTH = fixtureJson<HealthResponse>('outage/health.json')

function showAdmin(llm?: HealthResponse['llm']) {
  health.mockResolvedValue(llm ? { ...HEALTH, llm: { provider: 'anthropic', model: 'claude-test', ...llm } } : HEALTH)
  return render(
    <AdminPanel base="/api" tab="overview" settings={{ apiBaseUrl: '/api', userId: 'u1', conversationId: '' }}
                streaming onSave={() => {}} inspector={null} theme="light" onToggleTheme={() => {}} />,
    { lang: 'en', wrapper: wrap() },
  )
}

describe('管理页的模型服务状态', () => {
  beforeEach(() => { health.mockReset() })

  it('服务在线但模型凭证被拒（录下来的 /health）：两件事分开报，并说清怎么恢复', async () => {
    expect(HEALTH.llm).toMatchObject({ available: false, unavailable_reason: 'credential_rejected', retry_after_s: null })
    expect(JSON.stringify(HEALTH.llm)).not.toMatch(/sk-|relay\.example|api_key|base_url/)   // 不带凭证和地址
    showAdmin()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Unavailable: the model provider rejected the API key (invalid or disabled).')
    expect(alert).toHaveTextContent('It does not recover by itself.')
    expect(alert).toHaveTextContent('restart the backend or call the protected POST /skills/reload')
    expect(alert).toHaveTextContent('not probed in the background')
    expect(alert).toHaveTextContent(`${HEALTH.llm!.blocked_calls} model calls were held back instead of being sent.`)
    expect(screen.getByText('Online')).toBeInTheDocument()          // 后端本身是在线的
    expect(screen.getByText('Model service').parentElement).toHaveTextContent('Unavailable')
  })

  it('限流：说还要等多久，不说成要人来处理', async () => {
    showAdmin({ available: false, unavailable_reason: 'rate_limited', retry_after_s: 41.3, blocked_calls: 0 })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Unavailable: the model provider is rate limiting requests.')
    expect(alert).toHaveTextContent('The next call is allowed through in about 42 seconds.')
    expect(alert).not.toHaveTextContent(/does not recover by itself|held back/)
  })

  it('可用时只报可用；老后端没报就写未报告', async () => {
    const first = showAdmin({ available: true, unavailable_reason: '', retry_after_s: null })
    expect((await screen.findByText('Model service')).parentElement).toHaveTextContent('Available')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    first.unmount()

    showAdmin({})
    expect((await screen.findByText('Model service')).parentElement).toHaveTextContent('Not reported')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('评测没跑完', () => {
  const REPORT: EvalReport = {
    release: 'r1', timestamp: '2026-10-04T21:00:00', pass_rate: 1, total: 2, passed: 2,
    avg_scores: { relevance: 0.9, accuracy: 0.9, completeness: 0.9, helpfulness: 0.9 },
    regressions: [], recommendations: ['评测未完成：模型服务不可用（credential_rejected），已中止。'],
    promoted: false, baseline: { release: 'r0', timestamp: '2026-09-01T00:00:00' },
    results: [
      { test_id: 'dialog_0', passed: true, scores: { relevance: 0.9 }, detail: 'ok', metadata: {} },
      { test_id: 'dialog_1', passed: true, scores: { relevance: 0.9 }, detail: 'ok', metadata: {} },
    ],
    incomplete: true, incomplete_reason: 'credential_rejected', planned: 5,
  }

  beforeEach(() => { runEval.mockReset() })

  it('标出未完成、完成了几项，不显示成"没有退化"', async () => {
    runEval.mockResolvedValueOnce(REPORT)
    render(<EvalPanel base="/api" />, { lang: 'en', wrapper: wrap() })
    await userEvent.setup().click(screen.getByRole('button', { name: 'Run eval' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('stopped early because the model service was unavailable')
    expect(alert).toHaveTextContent('the model provider rejected the API key')
    expect(alert).toHaveTextContent('2 of 5 planned items finished')
    expect(alert).toHaveTextContent('cannot be promoted')
    expect(screen.getByText('Incomplete')).toBeInTheDocument()
    expect(screen.getByText('Not compared. The run did not finish.')).toBeInTheDocument()
    expect(screen.queryByText('No regression against the baseline.')).not.toBeInTheDocument()
    expect(screen.getByText('dialog_1')).toBeInTheDocument()        // 已完成的结果还在
    expect(screen.queryByText(/评测未完成/)).not.toBeInTheDocument() // 后端那句不重复显示
  })

  it('一项都没跑完（录下来的 /eval/run 响应）：不画一排 0 分和"通过率 0%"', async () => {
    const report = fixtureJson<EvalReport>('outage/eval-incomplete.json')
    expect(report).toMatchObject({ incomplete: true, total: 0, results: [], promoted: false })
    runEval.mockResolvedValueOnce(report)
    render(<EvalPanel base="/api" />, { lang: 'en', wrapper: wrap() })
    await userEvent.setup().click(screen.getByRole('button', { name: 'Run eval' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('stopped before any item finished')
    expect(alert).toHaveTextContent(`${report.planned} items were planned`)
    expect(alert).toHaveTextContent('cannot be promoted')
    expect(screen.getByText('Incomplete')).toBeInTheDocument()
    expect(screen.queryByText(/Pass rate/)).not.toBeInTheDocument()
    expect(document.querySelector('.meter-row')).toBeNull()         // 没有分数条
    expect(screen.queryByText('No regression against the baseline.')).not.toBeInTheDocument()
  })

  it('跑完的一轮不受影响', async () => {
    runEval.mockResolvedValueOnce({ ...REPORT, incomplete: false, incomplete_reason: '', planned: 2, recommendations: [] })
    render(<EvalPanel base="/api" />, { lang: 'en', wrapper: wrap() })
    await userEvent.setup().click(screen.getByRole('button', { name: 'Run eval' }))
    expect(await screen.findByText('No regression against the baseline.')).toBeInTheDocument()
    expect(screen.queryByText('Incomplete')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
