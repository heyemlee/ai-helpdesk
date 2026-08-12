/**
 * 对话里员工转述前必须知道的三件事：依据了哪些知识、订单数据算不算数、
 * 工单建没建和有没有人被通知。以及学习模式对这一轮的讲解。
 *
 * 断言盯的是"只说后端说过的话"：后端没报的不显示成事实，
 * "已有人接手 / 会回电"这类话任何状态下都不出现。
 */
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@/test/render'
import { MessageBubble } from '../MessageBubble'
import { ScopeGuide } from '../ChatPanel'
import { setStaffKey } from '@/api/client'
import { applyChatResponse, restoreMessages, type ChatMessage } from '@/hooks/useChatStream'
import type { ChatResponse, ConversationEntry, RoutingConfig, Ticket } from '@/api/types'

const routing = { primary_agent: 'billing', supporting_agents: [], reason: 'r', confidence: 0.9,
                  domain_scores: { billing: 0.9, order: 0.2 } }

const turn = (over: Partial<ChatMessage>): ChatMessage => ({
  id: 'a1', role: 'assistant', content: 'Finance issues a revised invoice.', routing,
  meta: { intent: 'payment_issue', intent_strategy: 'ensemble', intent_confidence: 0.81,
          intent_source_scores: { rules: 0.5, embedding: 0.2, llm: 0.9 }, knowledge_used: true },
  result: { answer_confidence: 0.82, latency_ms: 4200, policy_violations: [] },
  ...over,
})

const SOURCES = [
  { doc_id: 'legacy_56f0', chunk_id: 'c1', title: 'invoice', doc_title: 'Quotes Invoices and Change Orders',
    passage: 0, score: 0.41, retrieval: 'hybrid', excerpt: '更正由财务提供 revised invoice 或 credit memo。' },
  { doc_id: '', chunk_id: 'c2', title: 'orphan', passage: 2, score: 0.2, retrieval: 'dense', excerpt: '没有归到文档的片段。' },
]

const NEVER_CLAIMED = /已转人工|人工已收到|已通知|马上回电|稍后.*联系|has been notified|will call|call you back|someone (has )?received/i

describe('知识依据', () => {
  it('列出本轮用到的片段，能点回所在文档', () => {
    render(<MessageBubble message={turn({ meta: { ...turn({}).meta, knowledge_sources: SOURCES } })} />, { lang: 'en' })
    const sources = screen.getByText('Knowledge used').closest('details')!
    expect(within(sources).getByText('2 passages')).toBeInTheDocument()

    const link = within(sources).getByRole('link', { name: 'Quotes Invoices and Change Orders' })
    expect(link).toHaveAttribute('href', '#/knowledge/doc/legacy_56f0')
    expect(within(sources).getByText('Passage 1')).toBeInTheDocument()
    expect(within(sources).getByText(/revised invoice 或 credit memo/)).toBeInTheDocument()
    // 没有文档归属的片段不出一个点不开的链接，并说明它没关联到文档
    expect(within(sources).queryByRole('link', { name: 'orphan' })).not.toBeInTheDocument()
    expect(within(sources).getByText(/not linked to a document/)).toBeInTheDocument()
  })

  it('后端只报了 knowledge_used 而没报是哪几段时，不编一个依据列表', () => {
    render(<MessageBubble message={turn({})} />, { lang: 'en' })
    expect(screen.queryByText('Knowledge used')).not.toBeInTheDocument()
  })

  it('回复还在流的时候不提前显示依据和处置', () => {
    render(<MessageBubble message={turn({
      streaming: true, result: undefined, meta: { ...turn({}).meta, knowledge_sources: SOURCES },
    })} />, { lang: 'en' })
    expect(screen.queryByText('Knowledge used')).not.toBeInTheDocument()
    expect(screen.getByText('Quotes & Billing Agent')).toBeInTheDocument()
  })
})

describe('订单数据', () => {
  const withData = (business_data: Record<string, unknown>) =>
    turn({ meta: { ...turn({}).meta, business_data } })

  it('查到并通过校验：说明是实时查的、查了哪几个库', () => {
    render(<MessageBubble message={withData({ outcome: 'evidence', databases: ['mes_db', 'erp_db'] })} />, { lang: 'en' })
    expect(screen.getByText('Order data was looked up live (mes_db, erp_db).')).toBeInTheDocument()
  })

  it.each([
    ['unverified', /did not pass verification\. Confirm it before you quote it/],
    ['failed', /lookup did not complete.*not system-verified/],
    ['unavailable', /lookup did not complete.*not system-verified/],
  ])('%s：提醒员工这次的订单信息不能当已核实的讲', (outcome, expected) => {
    render(<MessageBubble message={withData({ outcome, attempted: true })} />, { lang: 'en' })
    expect(screen.getByText(expected)).toBeInTheDocument()
  })

  it.each(['skipped', 'not_configured', 'not_applicable'])('%s：没查不是需要提醒的事，默认不显示', (outcome) => {
    const { container } = render(<MessageBubble message={withData({ outcome })} />, { lang: 'en' })
    expect(container.querySelector('.turn-note, .note')).toBeNull()
  })
})

describe('转人工与工单', () => {
  const handoff = (result: Record<string, unknown>) => turn({
    result: { ...turn({}).result, handoff: true, handoff_reason: 'Agent 自评置信度 0.65 低于 0.7', ...result },
  })

  it('工单建了：给工单号、状态和详情链接；没配通知就说没有人收到提醒', () => {
    render(<MessageBubble message={handoff({
      ticket_id: 'tk_90390d64adba', ticket_status: 'open', ticket_notification: 'not_configured',
    })} />, { lang: 'en' })
    const card = screen.getByText('Needs human follow-up').closest('.ticket-note') as HTMLElement
    expect(within(card).getByText('tk_90390d64adba')).toBeInTheDocument()
    expect(within(card).getByText('Open')).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', '#/tickets/tk_90390d64adba')
    expect(within(card).getByText(/nobody has been alerted/)).toBeInTheDocument()
    expect(card.textContent).not.toMatch(NEVER_CLAIMED)
  })

  it.each(['assigned', 'resolved', 'cancelled'])(
    '工单已经流转到 %s：卡片跟着现在的状态说，不再写"需要跟进""在等处理"', (ticket_status) => {
      render(<MessageBubble message={handoff({
        ticket_id: 'tk_1ed49a633e04', ticket_status, ticket_notification: 'not_configured',
      })} />, { lang: 'en' })
      const card = screen.getByText('Ticket for human follow-up').closest('.ticket-note') as HTMLElement
      expect(within(card).getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', '#/tickets/tk_1ed49a633e04')
      expect(card).not.toHaveTextContent('Needs human follow-up')
      expect(card).not.toHaveTextContent(/waiting in Tickets|nobody has been alerted/)
      expect(card.textContent).not.toMatch(NEVER_CLAIMED)
    })

  it('通知入了队只说入队，不说送达', () => {
    render(<MessageBubble message={handoff({
      ticket_id: 'tk_1', ticket_status: 'open', ticket_notification: 'queued',
    })} />, { lang: 'en' })
    expect(screen.getByText('A notification is queued. Delivery has not been confirmed.')).toBeInTheDocument()
    expect(screen.queryByText(/reached the configured channel/)).not.toBeInTheDocument()
  })

  it('判了转人工但工单没建成：明确报出来，不显示成已经交接', () => {
    render(<MessageBubble message={handoff({ ticket_id: null, ticket_status: 'not_created' })} />, { lang: 'en' })
    expect(screen.getByRole('alert')).toHaveTextContent('no ticket could be created')
    expect(screen.queryByRole('link', { name: 'View ticket' })).not.toBeInTheDocument()
  })

  it('老后端只给工单号：显示工单和链接，通知一个字不猜', () => {
    render(<MessageBubble message={handoff({ ticket_id: 'tk_old' })} />, { lang: 'en' })
    const card = screen.getByText('Needs human follow-up').closest('.ticket-note') as HTMLElement
    expect(within(card).getByRole('link', { name: 'View ticket' })).toBeInTheDocument()
    expect(card.querySelector('.ticket-note-sub')).toBeNull()
  })

  it('刷新后恢复的历史里，转人工的那条回复仍然带着工单链接', () => {
    const entry = (seq: number, role: string, request_id: string): ConversationEntry => ({
      seq, role, request_id, conv_id: 'c1', user_id: 'u', content: `m${seq}`, agent_type: role === 'user' ? '' : 'handoff', created_at: seq,
    })
    const tickets = [{ id: 'tk_7', request_id: 'req-2', conv_id: 'c1' }] as Ticket[]
    const restored = restoreMessages(
      [entry(1, 'user', 'req-1'), entry(2, 'assistant', 'req-1'), entry(3, 'user', 'req-2'), entry(4, 'assistant', 'req-2')],
      tickets,
    )
    // 只有建了工单的那一轮的助手消息带工单号；用户消息和别的轮次都不带
    expect(restored.map((m) => m.ticketId)).toEqual([undefined, undefined, undefined, 'tk_7'])
    expect(restoreMessages([entry(1, 'assistant', '')], [{ id: 'tk_x', request_id: '' }] as Ticket[])[0]!.ticketId)
      .toBeUndefined()            // 空的 request_id 对不上任何工单

    render(<MessageBubble message={restored[3]!} />, { lang: 'en' })
    expect(screen.getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', '#/tickets/tk_7')
    expect(screen.getByText('Earlier')).toBeInTheDocument()
  })

  it.each(['en', 'zh'] as const)('%s 界面下任何状态都不出现"已有人接手/会回电"这类说法', (lang) => {
    for (const notification of ['not_configured', 'queued', undefined]) {
      const { container, unmount } = render(<MessageBubble explain message={handoff({
        ticket_id: 'tk_1', ticket_status: 'open', ticket_notification: notification,
      })} />, { lang })
      const own = [...container.querySelectorAll('.ticket-note, .explain, .turn-role')]
        .map((el) => el.textContent).join(' ')
      expect(own).not.toMatch(NEVER_CLAIMED)
      unmount()
    }
  })
})

describe('学习模式', () => {
  const config = { supporting_min_score: 0.45, supporting_min_ratio: 0.55, clarify_confidence_threshold: 0.5 } as RoutingConfig

  it('默认不出现；打开后按本轮实际发生的事逐步讲', () => {
    const message = turn({
      routing: { ...routing, supporting_agents: ['order'], domain_scores: { billing: 0.9, order: 0.6 } },
      meta: {
        ...turn({}).meta, knowledge_sources: SOURCES,
        knowledge: { attempted: true, mode: 'rewrite_rerank', reranked: true, corrected: true, evidence_ok: false },
        business_data: { enabled: true, attempted: true, outcome: 'evidence', databases: ['erp_db'], trace_id: 't-9' },
      },
    })
    const { rerender } = render(<MessageBubble message={message} />, { lang: 'en' })
    expect(screen.queryByText('What happened in this turn')).not.toBeInTheDocument()

    rerender(<MessageBubble message={message} explain routingConfig={config} onInspect={() => {}} />)
    const rows = [...document.querySelectorAll('.explain-row')].map((r) => r.textContent ?? '')
    expect(rows.map((r) => r.split(/(?<=^[A-Za-z ]+)(?=[A-Z])/)[0])).toHaveLength(6)
    const [intent, kb, route, data, check, handoffRow] = rows

    expect(intent).toMatch(/payment_issue.*confidence 0\.81.*rules 0\.50.*embedding 0\.20.*model 0\.90/)
    expect(kb).toMatch(/Rewrote the question.*2 passages from: Quotes Invoices and Change Orders, orphan\./)
    expect(kb).toMatch(/re-ranked.*one follow-up search ran.*judged insufficient/)
    expect(route).toMatch(/Quotes & Billing Agent scored highest \(0\.90\).*Order \(0\.60\)/)
    expect(route).toMatch(/threshold this turn was 0\.50/)            // max(0.45, 0.9 × 0.55)
    expect(data).toMatch(/SQL Agent, which queried erp_db.*passed verification.*trace t-9/)
    expect(check).toMatch(/Nothing was removed/)
    expect(handoffRow).toMatch(/Not needed\. Combined confidence was 0\.82/)
    expect(screen.getByRole('button', { name: 'Open orchestration inspector' })).toBeInTheDocument()
  })

  it.each([
    [{ enabled: false, attempted: false, outcome: 'not_configured' }, /Not connected.*nothing was queried/],
    [{ enabled: true, attempted: false, outcome: 'skipped' }, /Not queried\. The question does not ask about a specific order/],
    [{ enabled: true, attempted: true, outcome: 'unavailable', reason: 'ReadTimeout' }, /did not return a result \(ReadTimeout\)/],
    [{ enabled: true, attempted: true, outcome: 'unverified', databases: ['erp_db'] }, /did not pass verification.*not to present it as confirmed/],
  ])('订单数据 %o 的讲法', (business_data, expected) => {
    render(<MessageBubble explain message={turn({ meta: { ...turn({}).meta, business_data } })} />, { lang: 'en' })
    expect(screen.getByText('Order data').closest('li')).toHaveTextContent(expected)
  })

  it('没检索、快通道、被切句、转人工，各讲各的', () => {
    render(<MessageBubble explain message={turn({
      routing: { primary_agent: 'handoff', supporting_agents: [], reason: 'r', confidence: 1 },
      meta: { intent: 'human_handoff', intent_strategy: 'fast_path', knowledge_used: false,
              knowledge: { attempted: false }, business_data: { enabled: true, attempted: false, outcome: 'skipped' } },
      result: {
        answer_confidence: 0.3, handoff: true, handoff_reason: '用户明确要求人工', ticket_id: 'tk_7',
        policy_violations: [{ rule: 'lead_time_commitment', label: 'l', evidence: 'e', sentence: 'Tomorrow.', reason: 'r' }],
      },
    })} />, { lang: 'en' })
    const row = (label: string) => screen.getByText(label, { selector: '.explain-row > b' }).closest('li')!
    expect(row('Intent')).toHaveTextContent(/keyword rule recognised this as human_handoff.*no model call/)
    expect(row('Knowledge')).toHaveTextContent('Not searched.')
    expect(row('Specialist')).toHaveTextContent('Sent straight to human handoff')
    expect(row('Answer check')).toHaveTextContent('Removed 1 sentence: Unsupported lead-time commitment.')
    expect(row('Handoff')).toHaveTextContent(/A handoff was decided.*用户明确要求人工.*Ticket created: tk_7/)
  })

  it('老后端没报检索和委派经过时，说"没有报告"而不是替它猜', () => {
    render(<MessageBubble explain message={turn({})} />, { lang: 'en' })
    expect(screen.getByText('Knowledge').closest('li')).toHaveTextContent('This server does not report which ones')
    expect(screen.getByText('Order data').closest('li')).toHaveTextContent('does not report whether order data was queried')
  })
})

describe('关闭流式', () => {
  /** /chat 的非流式响应：没有单独的 routing 事件，主辅 Agent 在响应体里。 */
  const response = {
    conv_id: 'c1', response: 'Finance issues a revised invoice.', intent: 'payment_issue',
    intent_group: 'billing', agent_type: 'billing', agent_types: ['billing'], primary_agent: 'billing',
    supporting_agents: ['order'], routing_reason: 'r', routing_confidence: 0.9, escalated: false,
    latency_ms: 4200, knowledge_used: true, entities: {}, intent_confidence: 0.8,
    intent_source_scores: {}, intent_strategy: 'ensemble', intent_latency_ms: 3, handoff: true,
    handoff_reason: 'x', needs_clarification: false, answer_confidence: 0.6, policy_violations: [],
    ticket_id: 'tk_9', ticket_status: 'open', ticket_notification: 'not_configured',
    knowledge_sources: SOURCES, knowledge: { attempted: true, mode: 'direct' },
    business_data: { enabled: true, attempted: false, outcome: 'skipped' },
  } satisfies ChatResponse

  it('回复照常显示：身份、正文、依据、工单都在，不停在"正在处理"', () => {
    const pending: ChatMessage = { id: 'a', role: 'assistant', content: '', streaming: false }
    const message = applyChatResponse(pending, response)
    expect(message.routing).toEqual({
      primary_agent: 'billing', supporting_agents: ['order'], reason: 'r', confidence: 0.9,
    })

    render(<MessageBubble message={message} />, { lang: 'en' })
    expect(screen.queryByText('Working on it…')).not.toBeInTheDocument()
    expect(screen.getByText('Quotes & Billing Agent')).toBeInTheDocument()
    expect(screen.getByText('Finance issues a revised invoice.')).toBeInTheDocument()
    expect(screen.getByText('Knowledge used')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', '#/tickets/tk_9')
  })

  it('响应里没给 primary_agent 时退回 agent_type', () => {
    const message = applyChatResponse({ id: 'a', role: 'assistant', content: '' },
      { ...response, primary_agent: '', agent_type: 'general' })
    expect(message.routing!.primary_agent).toBe('general')
  })

  /**
   * 实测撞到的：账款专员那次模型调用超时，编排器降级让通用 Agent 重答。响应里
   * primary_agent 是 billing、agent_type 是 general——回复却挂着"账款专员"的名字，
   * 学习模式也说"由账款专员处理"；刷新后审计日志恢复出来的同一条又成了通用助手。
   */
  describe('专家没答完、兜底 Agent 接手', () => {
    const fellBack = { ...response, supporting_agents: [], agent_type: 'general', agent_types: ['general'],
                       handoff: false, handoff_reason: '', ticket_id: '', ticket_status: '', ticket_notification: '' }
    const pending: ChatMessage = { id: 'a', role: 'assistant', content: '' }

    it('署名是实际作答的那个，并说明专家没答完', () => {
      render(<MessageBubble message={applyChatResponse(pending, fellBack)} />, { lang: 'en' })
      expect(screen.getByText('Showroom Desk Agent', { selector: '.turn-role' })).toBeInTheDocument()
      expect(screen.queryByText('Quotes & Billing Agent', { selector: '.turn-role' })).not.toBeInTheDocument()
      expect(screen.getByText(/The Quotes & Billing Agent did not finish this reply.*general assistant answered instead/))
        .toBeInTheDocument()
    })

    it('学习模式把"选了谁"和"谁答的"分开讲', () => {
      const message = { ...applyChatResponse(pending, fellBack),
                        routing: { ...routing, domain_scores: { billing: 0.9 } } }
      render(<MessageBubble explain message={message} />, { lang: 'en' })
      expect(screen.getByText('Specialist', { selector: '.explain-row > b' }).closest('li')).toHaveTextContent(
        'Quotes & Billing Agent was selected (0.90) but did not finish its reply. Showroom Desk Agent answered instead as the fallback.')
    })

    it.each([
      ['专家自己答的', response, {}],
      ['协作里的辅助 Agent', { ...fellBack, supporting_agents: ['general'] }, {}],
      ['转人工', { ...fellBack, handoff: true, ticket_id: 'tk_1' }, {}],
      ['澄清追问', { ...fellBack, needs_clarification: true }, {}],
      ['这个部署的兜底 Agent 不是它', fellBack, { fallback_agent: 'order' }],
    ])('%s：不当成兜底', (_name, res, config) => {
      render(<MessageBubble message={applyChatResponse(pending, res as ChatResponse)}
                            routingConfig={config as RoutingConfig} />, { lang: 'en' })
      expect(screen.queryByText(/did not finish this reply/)).not.toBeInTheDocument()
    })
  })
})

describe('准备阶段的进度', () => {
  const waiting = (progress: ChatMessage['progress']): ChatMessage =>
    ({ id: 'a', role: 'assistant', content: '', streaming: true, progress })
  const steps = () => [...document.querySelectorAll('.progress-steps li')]
    .map((li) => `${li.className}: ${li.textContent}`)

  it('还没有任何进度时只说在处理，不猜在做什么', () => {
    render(<MessageBubble message={waiting([])} />, { lang: 'en' })
    expect(screen.getByText('Working on it…')).toBeInTheDocument()
  })

  it('本系统自己的阶段按界面语言说：先理解问题，再说这一轮要查什么', () => {
    const progress = [
      { stage: 'helpdesk.understanding', label: '正在理解问题' },
      { stage: 'helpdesk.gathering', label: '正在查找回答需要的资料', knowledge: true, order_data: false },
    ]
    const { unmount } = render(<MessageBubble message={waiting(progress)} />, { lang: 'en' })
    expect(steps()).toEqual(['done: Understanding the question', 'active: Searching the knowledge base'])
    unmount()
    render(<MessageBubble message={waiting(progress)} />, { lang: 'zh' })
    expect(steps()).toEqual(['done: 正在理解问题', 'active: 正在检索知识库'])
  })

  it.each([
    [{ knowledge: true, order_data: true }, 'Searching the knowledge base and looking up order data'],
    [{ knowledge: false, order_data: true }, 'Looking up order data'],
  ])('查什么照后端报的说：%o', (flags, text) => {
    render(<MessageBubble message={waiting([
      { stage: 'helpdesk.gathering', label: 'fallback', ...flags },
    ])} />, { lang: 'en' })
    expect(steps()).toEqual([`active: ${text}`])
  })

  it('下游查库的细分阶段在推进时，"查找资料"仍算进行中——知识检索可能还没完', () => {
    render(<MessageBubble message={waiting([
      { stage: 'helpdesk.understanding', label: 'x' },
      { stage: 'helpdesk.gathering', label: 'x', knowledge: true, order_data: true },
      { stage: 'generation', label: '正在生成查询（ERP 订单库）', database: 'erp_db' },
      { stage: 'execution', label: '正在查询（ERP 订单库）', database: 'erp_db' },
    ])} />, { lang: 'en' })
    expect(steps()).toEqual([
      'done: Understanding the question',
      'active: Searching the knowledge base and looking up order data',
      'done: Writing the query for the ERP order database',
      'active: Querying the ERP order database',
    ])
  })

  it('下游认得出的阶段按界面语言说，不照搬后端的中文 label', () => {
    const progress = [
      { stage: 'routing', label: '正在判断问题类型' },
      { stage: 'retrieval', label: '正在检索知识库' },
      { stage: 'planning', label: '正在规划查询步骤' },
      { stage: 'execution', label: '正在查询（生产库）', database: 'prod_db', step: 2 },
      { stage: 'repair', label: '查询出错，正在重试', database: 'prod_db', step: 2 },
    ]
    const { unmount } = render(<MessageBubble message={waiting(progress)} />, { lang: 'en' })
    expect(steps()).toEqual([
      'done: Working out what kind of question it is',
      'done: Finding the relevant tables',
      'done: Planning the query steps',
      'done: Querying the production database',
      'active: The query failed; fixing it and retrying',
    ])
    unmount()
    render(<MessageBubble message={waiting(progress)} />, { lang: 'zh' })
    expect(steps()).toEqual([
      'done: 正在判断问题类型',
      'done: 正在查找相关的数据表',
      'done: 正在规划查询步骤',
      'done: 正在查询（生产库）',
      'active: 查询出错，正在重试',
    ])
  })

  it('认不出的库照写标识符；没带库的查询步骤不硬拼', () => {
    render(<MessageBubble message={waiting([
      { stage: 'generation', label: '正在生成查询（crm_db）', database: 'crm_db' },
      { stage: 'execution', label: '正在查询' },
    ])} />, { lang: 'en' })
    expect(steps()).toEqual(['done: Writing the query for crm_db', 'active: Running the query'])
  })

  it('不认识的阶段用后端给的 label，不吞掉', () => {
    render(<MessageBubble message={waiting([{ stage: 'helpdesk.future', label: '以后新增的一步' }])} />, { lang: 'en' })
    expect(steps()).toEqual(['active: 以后新增的一步'])
  })
})

describe('请求失败', () => {
  const failed = (over: Partial<ChatMessage>): ChatMessage =>
    ({ id: 'a', role: 'assistant', content: '', error: '401 Unauthorized: 缺少或错误的 X-API-Key', ...over })

  it('没带钥匙：先说去哪填，再给后端原话', () => {
    render(<MessageBubble message={failed({ errorStatus: 401 })} />, { lang: 'en' })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('The reply could not be completed.')
    expect(alert).toHaveTextContent('This server requires a staff access key. Add it under Admin & diagnostics → Overview → Connection.')
    expect(alert).toHaveTextContent('401 Unauthorized: 缺少或错误的 X-API-Key')
    expect(within(alert).getByRole('link', { name: 'Open connection settings' })).toHaveAttribute('href', '#/admin')
  })

  it('带了钥匙还是 401：说钥匙被拒了，不再叫人"去填"', () => {
    setStaffKey('not-the-right-key')
    try {
      render(<MessageBubble message={failed({ errorStatus: 401 })} />, { lang: 'en' })
      expect(screen.getByRole('alert')).toHaveTextContent('The server rejected the staff access key saved in this tab.')
      expect(screen.getByRole('link', { name: 'Open connection settings' })).toBeInTheDocument()
    } finally {
      setStaffKey('')
    }
  })

  it.each([
    [0, 'Failed to fetch', 'The server could not be reached. Check that it is running.'],
    [503, '503 Service Unavailable', 'The service is still starting up. Try again in a moment.'],
  ])('状态码 %i：说该怎么办', (errorStatus, error, hint) => {
    render(<MessageBubble message={failed({ errorStatus, error })} />, { lang: 'en' })
    expect(screen.getByRole('alert')).toHaveTextContent(hint)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('认不出原因的（流中途报错、其它状态码）只给原话，不套一句不相干的建议', () => {
    render(<MessageBubble message={failed({ errorStatus: undefined, error: '模型超时' })} />, { lang: 'en' })
    expect(screen.getByRole('alert')).toHaveTextContent('The reply could not be completed. 模型超时')
    expect(screen.getByRole('alert')).not.toHaveTextContent('staff access key')
  })
})

describe('可以查询什么', () => {
  it('订单数据那一条照这个工作台实际接没接来说', () => {
    const { rerender } = render(<ScopeGuide capabilities={{ business_data: true, ticket_notifications: false }} />, { lang: 'en' })
    expect(screen.getByText(/Looked up live from the business systems, read-only/)).toBeInTheDocument()
    expect(screen.getByText(/not stored in the helpdesk/)).toBeInTheDocument()
    expect(screen.getByText(/does not mean someone has been notified/)).toBeInTheDocument()

    rerender(<ScopeGuide capabilities={{ business_data: false, ticket_notifications: false }} />)
    expect(screen.getByText(/Not connected in this workspace/)).toBeInTheDocument()

    rerender(<ScopeGuide />)
    expect(screen.getByText(/did not report whether order lookup is connected/)).toBeInTheDocument()
  })
})
