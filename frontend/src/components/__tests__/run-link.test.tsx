import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@/test/render'
import type { ChatMessage } from '@/hooks/useChatStream'
import { RoutingInspector } from '../RoutingInspector'
import { TurnExplanation } from '../TurnExplanation'

/**
 * 从对话的一轮跳到 SQL Agent 那边的那次查询（#/data/runs/<run_id>）。
 * run_id 由员工实例记在 business_data（和工具循环那次调用的 trace）里；没有就不画链接。
 */
const RESULT = {
  conv_id: 'c1', response: '在途。', agent_type: 'order', agent_types: ['order'], primary_agent: 'order',
  supporting_agents: [], routing_reason: 'r', routing_confidence: 0.9, answer_confidence: 0.9,
}

function turn(businessData: Record<string, unknown>, trajectory: unknown[] = []): ChatMessage {
  return {
    id: 'm1', role: 'assistant', content: '在途。',
    meta: {
      conv_id: 'c1', turn_id: 't1', intent: 'order_status', intent_group: 'order', intent_confidence: 0.9,
      intent_strategy: 'ensemble', intent_latency_ms: 1, intent_source_scores: {}, entities: {},
      knowledge_used: false, business_data: businessData,
    },
    routing: { primary_agent: 'order', supporting_agents: [], reason: 'r', confidence: 0.9 },
    result: { ...RESULT, ...(trajectory.length ? { trajectory, answer_usage: { model_calls: 2, input_tokens: 1, output_tokens: 1 } } : {}) },
  } as unknown as ChatMessage
}

const DELEGATED = { enabled: true, attempted: true, outcome: 'evidence', trace_id: 'tr-1',
                    run_id: 'a0c9ee5033f04215986629f1febc5516', databases: ['erp_db'] }

describe('从对话的一轮跳到那次查询', () => {
  it('学习模式：委派过的那一行带"查看这次查询"', () => {
    render(<TurnExplanation message={turn(DELEGATED)} />, { lang: 'en' })
    expect(screen.getByRole('link', { name: 'See this query' }))
      .toHaveAttribute('href', '#/data/runs/a0c9ee5033f04215986629f1febc5516')
  })

  it('编排检查器：订单数据委派那一节带同一个链接', () => {
    render(<RoutingInspector message={turn(DELEGATED)} />, { lang: 'zh' })
    const section = screen.getByText('订单数据委派').closest('section') as HTMLElement
    expect(within(section).getByText('erp_db')).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: '查看这次查询' }))
      .toHaveAttribute('href', '#/data/runs/a0c9ee5033f04215986629f1febc5516')
  })

  it('没有 run_id（老轮次、委派没发出去）就不画链接', () => {
    const { unmount } = render(<TurnExplanation message={turn({ ...DELEGATED, run_id: '' })} />, { lang: 'en' })
    expect(screen.queryByRole('link', { name: 'See this query' })).not.toBeInTheDocument()
    unmount()
    render(<RoutingInspector message={turn({ enabled: true, attempted: false, outcome: 'skipped' })} />, { lang: 'en' })
    expect(screen.queryByText('Order data delegation')).not.toBeInTheDocument()
  })

  it('工具循环里专家自己委派的那一次也能跳过去', () => {
    const call = { step: 1, call_id: 'c1', actor: 'model', agent: 'order', tool: 'order_data_query',
                   args: { query: 'J25' }, outcome: 'evidence', is_error: false, evidence: true, dedup: false,
                   latency_ms: 1200, trace: { trace_id: 'tr-2', run_id: 'b1b1' } }
    render(<TurnExplanation message={turn({ ...DELEGATED, run_id: '' }, [call])} />, { lang: 'en' })
    expect(screen.getByRole('link', { name: 'See this query' })).toHaveAttribute('href', '#/data/runs/b1b1')
  })
})
