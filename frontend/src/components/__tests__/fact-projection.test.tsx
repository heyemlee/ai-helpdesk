import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import type { ChatMessage } from '@/hooks/useChatStream'
import { RoutingInspector } from '../RoutingInspector'
import { TurnExplanation } from '../TurnExplanation'

const message: ChatMessage = {
  id: 'fact-projection', role: 'assistant', content: '系统记录已核对：配送中',
  result: { answer_origin: 'verified_fact_projection', answer_model_invoked: false,
    agent_type: 'system', agent_types: [], handoff: false, escalated: false, policy_violations: [] },
}

describe('verified fact projection', () => {
  it.each(['zh', 'en'] as const)('explains source checks without invented model confidence in %s', lang => {
    const { container } = render(<><TurnExplanation message={message} /><RoutingInspector message={message} /></>, { lang })
    expect(screen.getByText(lang === 'zh' ? /未调用回答模型/ : /No answer model was invoked/)).toBeInTheDocument()
    expect(screen.getByText(lang === 'zh' ? /草稿仍需员工审批/ : /draft requires staff approval/)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/综合置信度|Combined confidence|100%|1\.00|0\.00/)
  })
})
