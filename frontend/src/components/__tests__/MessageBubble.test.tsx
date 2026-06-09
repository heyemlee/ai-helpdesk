import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageBubble } from '../MessageBubble'
import type { ChatMessage } from '@/hooks/useChatStream'

describe('MessageBubble', () => {
  it('用户消息不带路由徽章', () => {
    const m: ChatMessage = { id: '1', role: 'user', content: '我要退款' }
    render(<MessageBubble message={m} />)
    expect(screen.getByText('我要退款')).toBeInTheDocument()
    expect(screen.queryByText(/主 /)).not.toBeInTheDocument()
  })

  it('助手消息展示主辅 Agent 和置信度', () => {
    const m: ChatMessage = {
      id: '2', role: 'assistant', content: '好的',
      routing: { primary_agent: 'technical', supporting_agents: ['billing'], reason: '', confidence: 1 },
      result: { answer_confidence: 0.82 } as never,
    }
    render(<MessageBubble message={m} />)
    expect(screen.getByText('主 技术')).toBeInTheDocument()
    expect(screen.getByText('辅 账单')).toBeInTheDocument()
    expect(screen.getByText(/置信 0\.82/)).toBeInTheDocument()
  })

  it('错误消息用错误样式渲染，不显示空气泡', () => {
    const m: ChatMessage = { id: '3', role: 'assistant', content: '', error: '502 Bad Gateway' }
    render(<MessageBubble message={m} />)
    expect(screen.getByText('502 Bad Gateway')).toBeInTheDocument()
  })
})
