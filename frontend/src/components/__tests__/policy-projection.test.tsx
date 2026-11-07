import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import type { ChatMessage } from '@/hooks/useChatStream'
import { RoutingInspector } from '../RoutingInspector'
import { TurnExplanation } from '../TurnExplanation'

const message: ChatMessage = {
  id: 'policy-projection', role: 'assistant', content: '已发布政策原文',
  result: { answer_origin: 'verified_policy_projection', answer_model_invoked: false,
    agent_type: 'system', agent_types: [], handoff: false, escalated: false, policy_violations: [] },
}

describe('verified policy projection', () => {
  it.each(['zh', 'en'] as const)('shows policy authority and staff approval without invented confidence in %s', lang => {
    const { container } = render(<><TurnExplanation message={message} /><RoutingInspector message={message} /></>, { lang })
    expect(screen.getByText(lang === 'zh' ? /未调用回答模型/ : /No answer model was invoked/)).toBeInTheDocument()
    expect(screen.getByText(lang === 'zh' ? /草稿仍需员工审批/ : /draft requires staff approval/)).toBeInTheDocument()
    expect(screen.getByText(lang === 'zh' ? /文档版本、所选范围/ : /document revision and selected scope/)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/订单及来源|综合置信度|Combined confidence|100%|1\.00/)
  })
})
