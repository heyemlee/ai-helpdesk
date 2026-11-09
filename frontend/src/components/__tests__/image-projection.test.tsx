import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import type { ChatMessage } from '@/hooks/useChatStream'
import { RoutingInspector } from '../RoutingInspector'
import { TurnExplanation } from '../TurnExplanation'

describe('verified image projection', () => {
  it.each(['zh', 'en'] as const)('explains checked materials and pending staff approval in %s', lang => {
    const message: ChatMessage = {
      id: 'image-projection', role: 'assistant', content: '图片已核对',
      routing: { primary_agent: '', supporting_agents: [], confidence: 0, reason: '已核对图片材料' },
      result: { answer_origin: 'verified_image_projection', answer_model_invoked: false,
        // Stale presentation fields must not manufacture a score for this mode.
        answer_confidence: 1, agent_type: 'system', agent_types: [], handoff: false, escalated: false, policy_violations: [] },
    }
    const { container } = render(<><TurnExplanation message={message} /><RoutingInspector message={message} /></>, { lang })
    expect(screen.getByText(lang === 'zh' ? /图片材料已逐项核对；草稿仍需员工审批/ : /Image materials have been checked; the draft still requires staff approval/)).toBeInTheDocument()
    expect(screen.getByText(lang === 'zh' ? /使用图片核对时已确认的业务记录/ : /business records already checked against the image/)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/综合置信度|Combined confidence|100%|1\.00|0\.00|HMAC|SQL dispatch|签名|模型审核/)
  })
})
