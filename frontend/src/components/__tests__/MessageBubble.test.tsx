import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import { MessageBubble } from '../MessageBubble'
import type { ChatMessage } from '@/hooks/useChatStream'

describe('MessageBubble', () => {
  it('用户消息不带路由徽章', () => {
    const m: ChatMessage = { id: '1', role: 'user', content: '我要退款' }
    render(<MessageBubble message={m} />)
    expect(screen.getByText('我要退款')).toBeInTheDocument()
    expect(screen.queryByText(/主 /)).not.toBeInTheDocument()
  })

  it('助手消息展示答复 Agent 身份、协作 Agent 与置信度', () => {
    const m: ChatMessage = {
      id: '2', role: 'assistant', content: '好的',
      routing: { primary_agent: 'technical', supporting_agents: ['billing'], reason: '', confidence: 1 },
      result: { answer_confidence: 0.82 } as never,
    }
    render(<MessageBubble message={m} />)
    // 多 Agent 产品里"谁答的"是第一信息，用对外角色名而不是内部类型名
    expect(screen.getByText(/产品与安装 Agent/)).toBeInTheDocument()
    expect(screen.getByText(/账单/)).toBeInTheDocument()
    // 遥测（耗时/置信度）只属于检查器，不该出现在阅读流里
    expect(screen.queryByText(/置信/)).not.toBeInTheDocument()
  })

  it('routing 未到时不编造身份，只显示处理中', () => {
    const m: ChatMessage = { id: '5', role: 'assistant', content: '', streaming: true }
    render(<MessageBubble message={m} />)
    expect(screen.getByText(/正在处理/)).toBeInTheDocument()
  })

  it('result 事件到达前不显示总耗时与综合置信度', () => {
    // SSE 序列是 meta → routing → delta* → result。
    // latency_ms 是总耗时、answer_confidence 是对成品答案算的，两者都只在 result 里到达，
    // 正文还在流的时候把它们画出来 = 展示此刻不可能知道的事实。
    const m: ChatMessage = {
      id: '4', role: 'assistant', content: '台面报价我这里不能', streaming: true,
      meta: { intent_strategy: 'fast_path' } as never,
      routing: { primary_agent: 'billing', supporting_agents: [], reason: '', confidence: 1 },
    }
    render(<MessageBubble message={m} />)
    expect(screen.getByText(/报价与账款 Agent/)).toBeInTheDocument()
    expect(screen.queryByText(/置信/)).not.toBeInTheDocument()
    expect(screen.queryByText(/4\.88s|ms/)).not.toBeInTheDocument()
  })

  it('错误消息用错误样式渲染，不显示空气泡', () => {
    const m: ChatMessage = { id: '3', role: 'assistant', content: '', error: '502 Bad Gateway' }
    render(<MessageBubble message={m} />)
    expect(screen.getByText('502 Bad Gateway')).toBeInTheDocument()
  })
})

describe('准备阶段的进度', () => {
  // 委派查库要十几到四十秒。以前这段只有一个转圈，客户盯着不动的点看半分钟。
  it('有 progress 时铺成步骤，最后一条在进行中', () => {
    render(<MessageBubble message={{
      id: 'p1', role: 'assistant', content: '',
      progress: [
        { stage: 'routing', label: '正在判断问题类型' },
        { stage: 'execution', label: '正在查询（ERP 订单库）', database: 'erp_db' },
      ],
    }} />)
    expect(screen.getByText('正在判断问题类型')).toBeInTheDocument()
    const active = screen.getByText('正在查询（ERP 订单库）').closest('li')
    expect(active).toHaveClass('active')
    expect(screen.getByText('正在判断问题类型').closest('li')).toHaveClass('done')
  })

  it('没有 progress 时退回原来的转圈——非 A2A 传输时后端不发这个事件', () => {
    const { container } = render(<MessageBubble message={{ id: 'p2', role: 'assistant', content: '' }} />)
    expect(container.querySelector('.thinking')).toBeTruthy()
    expect(container.querySelector('.progress-steps')).toBeNull()
  })
})

describe('工具循环', () => {
  const routing = { primary_agent: 'order', supporting_agents: [], reason: '', confidence: 1 }
  const call = {
    stage: 'helpdesk.tool_call', label: '正在查业务系统',
    tool: 'order_data_query', call_id: 'c1', query: 'SO-1 补件进度',
  }
  const done = {
    stage: 'helpdesk.tool_result', label: '查询完成',
    tool: 'order_data_query', call_id: 'c1', outcome: 'evidence', evidence: true,
  }

  it('作答途中在查东西时说正在查什么，查完就不再显示', () => {
    // 查业务系统一次十几秒到四十多秒，这段时间回复不动；不说一声看起来像卡住了
    const base: ChatMessage = { id: 't1', role: 'assistant', content: '我帮您查一下。', streaming: true, routing }
    const { rerender } = render(<MessageBubble message={{ ...base, progress: [call] }} />)
    expect(screen.getByText('正在查业务系统：SO-1 补件进度')).toBeInTheDocument()

    rerender(<MessageBubble message={{ ...base, progress: [call, done] }} />)
    expect(screen.queryByText(/正在查业务系统/)).not.toBeInTheDocument()
  })

  it('回复结束后不再显示状态行，哪怕没收到结束的那一条', () => {
    render(<MessageBubble message={{
      id: 't2', role: 'assistant', content: '已查到。', streaming: false, routing, progress: [call],
    }} />)
    expect(screen.queryByText(/正在查业务系统/)).not.toBeInTheDocument()
  })

  const trajectory = [
    {
      step: 0, call_id: '', actor: 'rule' as const, agent: null, tool: 'order_data_query',
      args: { query: 'SO-1 补件到哪了' }, outcome: 'evidence', is_error: false, evidence: true,
      dedup: false, latency_ms: null, trace: {},
    },
    {
      step: 1, call_id: 'c1', actor: 'model' as const, agent: 'order', tool: 'order_data_query',
      args: { query: 'SO-1 补件进度' }, outcome: 'evidence', is_error: false, evidence: true,
      dedup: false, latency_ms: 18000, trace: {},
    },
    {
      step: 1, call_id: 'c2', actor: 'model' as const, agent: 'order', tool: 'knowledge_search',
      args: { query: '补件流程' }, outcome: 'found', is_error: false, evidence: true,
      dedup: true, latency_ms: 4, trace: { sources: [{}, {}] },
    },
  ]

  function answered(result: Record<string, unknown>): ChatMessage {
    return {
      id: 't3', role: 'assistant', content: '已查到。', routing,
      result: { answer_confidence: 0.9, policy_violations: [], ...result } as never,
    }
  }

  it('学习模式列出专家作答时自己查了什么', () => {
    render(<MessageBubble explain message={answered({
      answer_usage: { model_calls: 2, input_tokens: 50, output_tokens: 20 }, trajectory,
    })} />)
    const row = screen.getByText('作答时查询').closest('li')!
    expect(row).toHaveTextContent('专家作答时自己查了 2 次')
    expect(row).toHaveTextContent('业务系统 “SO-1 补件进度” → 查到可作依据的数据')
    expect(row).toHaveTextContent('知识库 “补件流程” → 找到 2 段 （复用了本轮之前的结果）')
    expect(row).toHaveTextContent('作答阶段调用模型 2 次')
    // 规则预取那一条不算"专家自己查的"
    expect(row).not.toHaveTextContent('SO-1 补件到哪了')
  })

  it('循环开着但没有再调工具，照实说', () => {
    render(<MessageBubble explain message={answered({
      answer_usage: { model_calls: 1, input_tokens: 20, output_tokens: 8 }, trajectory: trajectory.slice(0, 1),
    })} />)
    expect(screen.getByText('作答时查询').closest('li'))
      .toHaveTextContent('专家直接用回答前预取的资料作答，没有再调工具。')
  })

  it('循环关着（老后端或开关没开）时没有这一行', () => {
    render(<MessageBubble explain message={answered({})} />)
    expect(screen.queryByText('作答时查询')).not.toBeInTheDocument()
  })

  it('英文界面里同样说清楚，复数按次数走', () => {
    render(<MessageBubble explain message={answered({
      answer_usage: { model_calls: 2 }, trajectory,
    })} />, { lang: 'en' })
    const row = screen.getByText('Lookups while answering').closest('li')!
    expect(row).toHaveTextContent('The specialist looked things up itself 2 times:')
    expect(row).toHaveTextContent('Business systems “SO-1 补件进度” → returned data that can back the answer')
    expect(row).toHaveTextContent('Knowledge base “补件流程” → found 2 passages (reused an earlier result)')
    expect(row).toHaveTextContent('2 model calls while answering.')
  })
})
