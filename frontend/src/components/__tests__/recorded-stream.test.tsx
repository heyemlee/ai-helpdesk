/**
 * 用真后端录下来的 SSE 与审计日志回放渲染。
 * 夹具怎么来的、什么时候录的：src/test/fixtures/manifest.json。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RoutingInspector } from '../RoutingInspector'
import { MessageBubble } from '../MessageBubble'
import { restoreMessages } from '@/hooks/useChatStream'
import { fixtureJson, foldStream } from '@/test/fixtures'
import type { ConversationResponse } from '@/api/types'

const pricing = foldStream('chat-stream-pricing.sse')
const order = foldStream('chat-stream-order.sse')

describe('录制的 /chat/stream 回放', () => {
  it('夹具本身长成真后端的样子：全套实体键、空数组、长 routing_reason', () => {
    // 这几条是「手写夹具永远不会有」的形状；夹具要是没了它们，测试就在测假的东西
    expect(Object.keys(pricing.meta!.entities!)).toEqual(
      expect.arrayContaining(['order_id', 'product', 'date', 'amount', 'item_code']),
    )
    expect(Object.values(pricing.meta!.entities!).every((v) => v.length === 0)).toBe(true)
    expect(pricing.routing!.reason.length).toBeGreaterThan(100)
    expect(pricing.result!.request_id).toMatch(/^[0-9a-f]{12}$/)
    expect(order.meta!.entities!.order_id).toEqual(['SO-2026-0912'])
  })

  it('实体全空时检查器不渲染任何空 chip', () => {
    render(<RoutingInspector message={pricing} />)
    for (const k of ['order_id', 'product', 'date', 'amount', 'item_code']) {
      expect(screen.queryByText(new RegExp(`^${k}:\\s*$`))).not.toBeInTheDocument()
    }
    expect(screen.getByText('总耗时')).toBeInTheDocument()
    expect(screen.getByText(pricing.result!.request_id!)).toBeInTheDocument()
    expect(screen.getByText(pricing.routing!.reason)).toBeInTheDocument()
  })

  it('抽到订单号时只渲染有值的那一个 chip，转人工连工单一起显示', () => {
    render(<RoutingInspector message={order} />)
    expect(screen.getByText('order_id: SO-2026-0912')).toBeInTheDocument()
    expect(screen.queryByText(/^product:\s*$/)).not.toBeInTheDocument()
    expect(screen.getByText('已转人工')).toBeInTheDocument()
    expect(screen.getByText(order.result!.ticket_id!)).toBeInTheDocument()
  })

  it('消息气泡按真事件序列渲染出身份、正文和讲解行', () => {
    render(<MessageBubble message={pricing} explain />)
    expect(screen.getByText('报价与账款 Agent')).toBeInTheDocument()
    for (const row of ['意图', '路由', '出站', '处置', '耗时']) {
      expect(screen.getByText(row)).toBeInTheDocument()
    }
    // 正文来自 result.response（流式 delta 拼出来的和它一致）
    const firstLine = pricing.result!.response!.split('\n')[0]!.replace(/[*_`]/g, '').slice(0, 12)
    expect(screen.getByText(new RegExp(firstLine))).toBeInTheDocument()
  })
})

describe('录制的 /conversations/{conv_id} 恢复', () => {
  const conv = fixtureJson<ConversationResponse>('conversation.json')
  const restored = restoreMessages(conv.messages)

  it('按 seq 升序、user/assistant 交替、助手消息带作答 Agent', () => {
    expect(restored.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant'])
    expect(restored.map((m) => m.id)).toEqual(conv.messages.map((e) => `restored-${e.seq}`))
    expect(restored.filter((m) => m.role === 'assistant').map((m) => m.agentType)).toEqual(['billing', 'order'])
    expect(restored.every((m) => m.restored)).toBe(true)
  })

  it('乱序返回也按 seq 排回去', () => {
    const shuffled = [...conv.messages].reverse()
    expect(restoreMessages(shuffled).map((m) => m.id)).toEqual(restored.map((m) => m.id))
  })

  it('恢复的助手消息不走「正在编排」，显示 Agent 名和「历史」标记', () => {
    render(<MessageBubble message={restored[1]!} />)
    expect(screen.getByText('报价与账款 Agent')).toBeInTheDocument()
    expect(screen.getByText('历史')).toBeInTheDocument()
    expect(screen.queryByText(/正在编排/)).not.toBeInTheDocument()
  })

  it('检查器对历史消息说清楚没有决策可看', () => {
    render(<RoutingInspector message={restored[1]} />)
    expect(screen.getByText(/历史消息/)).toBeInTheDocument()
  })
})
