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
