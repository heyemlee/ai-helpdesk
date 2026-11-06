import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import type { ChatMessage } from '@/hooks/useChatStream'
import { MessageBubble } from '../MessageBubble'
import { RoutingInspector } from '../RoutingInspector'
import { TurnExplanation } from '../TurnExplanation'

const message: ChatMessage = {
  id: 'verified-image', role: 'assistant', content: '图片已核实 [I1] · 订购数量: 5 pcs',
  routing: { primary_agent: 'order', supporting_agents: [], reason: '', confidence: 1 },
  result: {
    handoff: false, escalated: false, answer_confidence: .15,
    policy_violations: [{ rule: 'lead_time_commitment', label: '无依据承诺', sentence: '已被替换的中间答案',
      evidence: '原句', reason: '原答案策略拦截' }],
    handoff_review: { basis: 'answer_quality', outcome: 'replaced_by_verified_draft',
      model_answer_sha256: 'old', draft_sha256: 'final',
      original_handoff_reason: '原模型答案低置信度', original_answer_confidence: .15 },
  },
}

describe('verified Case draft handoff review', () => {
  it('shows the published draft without an old-answer redaction or handoff warning', () => {
    render(<MessageBubble message={message} />)
    expect(screen.getByText(/图片已核实/)).toBeInTheDocument()
    expect(screen.queryByText('已被替换的中间答案')).not.toBeInTheDocument()
    expect(screen.queryByText('需要人工跟进')).not.toBeInTheDocument()
  })

  it('keeps old confidence, violations and reason visibly scoped to the replaced answer', () => {
    render(<RoutingInspector message={message} />)
    expect(screen.getByText(/原模型置信度 0.15/, { selector: '.chip' })).toBeInTheDocument()
    expect(screen.getByText(/被替换模型答案的检查记录/)).toBeInTheDocument()
    expect(screen.getByText('无依据承诺')).toBeInTheDocument()
    expect(screen.getByText(/原模型答案低置信度/)).toBeInTheDocument()
    expect(screen.queryByText('需要人工跟进')).not.toBeInTheDocument()
  })

  it.each(['zh', 'en'] as const)('explains final-draft review in %s without claiming a new confidence', lang => {
    render(<TurnExplanation message={message} />, { lang })
    expect(screen.getByText(lang === 'zh' ? /最终草稿按有效证据生成/ : /final draft was generated from verified records/)).toBeInTheDocument()
    expect(screen.getByText(lang === 'zh' ? /原模型置信度 0.15 仅保留用于诊断/ : /Original model confidence 0.15 is retained for diagnosis/)).toBeInTheDocument()
  })
})
