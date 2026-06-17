import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
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

  it('转人工时显示原因', () => {
    render(
      <RoutingInspector
        message={{ ...base, result: { handoff: true, handoff_reason: '置信度不足' } as never }}
      />,
    )
    expect(screen.getByText('已转人工')).toBeInTheDocument()
    expect(screen.getByText('置信度不足')).toBeInTheDocument()
  })
})
