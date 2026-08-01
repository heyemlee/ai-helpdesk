import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import { RoutingInspector } from '../RoutingInspector'
import type { ChatMessage } from '@/hooks/useChatStream'

const base: ChatMessage = { id: '1', role: 'assistant', content: '好的' }

describe('RoutingInspector', () => {
  it('没有消息时给出提示而不是空白', () => {
    render(<RoutingInspector />)
    expect(screen.getByText(/发一条消息后/)).toBeInTheDocument()
  })

  it('规则快通道要显式标出"0 次 LLM"', () => {
    render(
      <RoutingInspector
        message={{
          ...base,
          meta: { intent: 'quote_pricing', intent_strategy: 'fast_path', intent_latency_ms: 1.4 } as never,
          routing: { primary_agent: 'billing', supporting_agents: [], reason: 'r', confidence: 1 },
        }}
      />,
    )
    expect(screen.getByText(/0 次 LLM/)).toBeInTheDocument()
  })

  it('跨域工单要同时标出主 Agent 和辅助 Agent', () => {
    render(
      <RoutingInspector
        message={{
          ...base,
          routing: {
            primary_agent: 'technical',
            supporting_agents: ['billing'],
            reason: 'r',
            confidence: 1,
            domain_scores: { technical: 1, billing: 0.62, order: 0, general: 0.1 },
          },
        }}
      />,
    )
    expect(screen.getByText('主 Agent', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('技术')).toBeInTheDocument()
    expect(screen.getByText('账单')).toBeInTheDocument()
  })

  it('答案策略拦截要列出被拦下的原文', () => {
    render(
      <RoutingInspector
        message={{
          ...base,
          result: {
            handoff: false,
            policy_violations: [{
              rule: 'lead_time_commitment',
              label: '无依据的时效承诺',
              evidence: '3个工作日',
              sentence: '退款会在3个工作日内到账。',
              reason: '未出现在检索结果中',
            }],
          } as never,
        }}
      />,
    )
    expect(screen.getByText('无依据的时效承诺')).toBeInTheDocument()
    expect(screen.getByText('3个工作日')).toBeInTheDocument()
  })

  // 真实后端每轮都回全套实体键，抽不到就是空数组——上面几个夹具一个都没带
  // entities，所以「五个空 chip 常驻」这条一直没人测出来。
  it('实体抽空时不渲染空 chip，只留有值的那几个', () => {
    render(
      <RoutingInspector
        message={{
          ...base,
          meta: {
            intent: 'order_status',
            entities: { order_id: ['SO-2026-0912'], product: [], date: [], amount: [], item_code: [] },
          } as never,
          routing: { primary_agent: 'order', supporting_agents: [], reason: 'r', confidence: 1 },
        }}
      />,
    )
    expect(screen.getByText('order_id: SO-2026-0912')).toBeInTheDocument()
    for (const k of ['product', 'date', 'amount', 'item_code']) {
      expect(screen.queryByText(new RegExp(`^${k}:\\s*$`))).not.toBeInTheDocument()
    }
  })

  it('转人工时显示原因', () => {
    render(
      <RoutingInspector
        message={{ ...base, result: { handoff: true, handoff_reason: '置信度不足' } as never }}
      />,
    )
    expect(screen.getByText('需要人工跟进')).toBeInTheDocument()
    expect(screen.getByText('置信度不足')).toBeInTheDocument()
  })
})

describe('RoutingInspector · 未发起与协作门槛', () => {
  const fastPath: ChatMessage = {
    ...base,
    meta: {
      intent: 'order_status', intent_strategy: 'fast_path', intent_latency_ms: 3,
      intent_source_scores: { rules: 0.94, embedding: 0, llm: 0 },
    } as never,
    routing: {
      primary_agent: 'order', supporting_agents: [], reason: 'r', confidence: 0.91,
      domain_scores: { order: 0.91, technical: 0.18, billing: 0.11 },
    } as never,
  }

  it('快通道下 embedding/llm 标为「未发起」，而不是画成 0.00 的空条', () => {
    // 后端在 fast_path 时于任何网络调用之前就返回，source_scores 里那两个 0.0
    // 是占位符不是测量值。画成 0.00 会被读成「发起了、得零分」。
    render(<RoutingInspector message={fastPath} />)
    expect(screen.getAllByText('未发起')).toHaveLength(2)
    expect(screen.queryByText('0.00')).not.toBeInTheDocument()
    expect(screen.getByText('0.94')).toBeInTheDocument()   // rules 是真测出来的
  })

  it('拿不到后端阈值时不画门槛线，绝不在前端写死常量', () => {
    const { container } = render(<RoutingInspector message={fastPath} />)
    expect(container.querySelector('.meter > .gate')).toBeNull()
    expect(screen.queryByText(/协作门槛/)).not.toBeInTheDocument()
  })

  it('拿到阈值后按 max(绝对分, 主分×比例) 画线，且不画在主 Agent 行上', () => {
    const { container } = render(
      <RoutingInspector message={fastPath} thresholds={{ minScore: 0.45, minRatio: 0.55 }} />,
    )
    // max(0.45, 0.91×0.55=0.5005) = 0.5005 → 50.05%
    const gates = container.querySelectorAll<HTMLElement>('.meter > .gate')
    expect(gates).toHaveLength(2)                       // 三个域，主 Agent 那行不画
    expect(gates[0]!.style.left.startsWith('50.0')).toBe(true)
    expect(container.querySelector('.legend .g')).not.toBeNull()
  })
})
