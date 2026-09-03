/**
 * 用真后端录下来的 SSE 与审计日志回放渲染。
 * 夹具怎么来的、什么时候录的：src/test/fixtures/manifest.json。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import { RoutingInspector } from '../RoutingInspector'
import { MessageBubble } from '../MessageBubble'
import { restoreMessages } from '@/hooks/useChatStream'
import { fixtureJson, foldStream } from '@/test/fixtures'
import type { ConversationResponse } from '@/api/types'

const pricing = foldStream('zh-chat-stream-pricing.sse')
const order = foldStream('zh-chat-stream-order.sse')
const enPricing = foldStream('en-chat-stream-pricing.sse')
const enOrder = foldStream('en-chat-stream-order.sse')

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
  const conv = fixtureJson<ConversationResponse>('zh-conversation.json')
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

// ── 英文对话（回复语言跟客户走之后的一条独立渲染路径）────────────────────────
//
// 英文正文配中文诊断串、英文转人工通知、markdown 结构也不一样。只录中文时
// 这条路径一行测试都没有——而它恰恰是新引入的那条。

describe('录制的英文 /chat/stream 回放', () => {
  it('夹具确实是英文正文 + 中文诊断串的混排', () => {
    const body = enPricing.result!.response!
    expect(/[一-鿿]/.test(body)).toBe(false)   // 正文不许漏中文
    expect(body.length).toBeGreaterThan(200)
    expect((body.match(/[A-Za-z]/g) ?? []).length).toBeGreaterThan(200)
    // 诊断串是给操作员看的，按设计保持中文口径（这里只断言它没跟着变英文）
    expect(enPricing.routing!.reason).toMatch(/intent=|scores=/)
  })

  it('英文提问同样抽得出订单号实体', () => {
    expect(enOrder.meta!.entities!.order_id).toEqual(['SO-2026-0912'])
  })

  it('英文回复整条渲染出来，不吞不截断', () => {
    render(<MessageBubble message={enPricing} onInspect={() => {}} />)
    const head = enPricing.result!.response!.slice(0, 40)
    expect(screen.getByText(new RegExp(head.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeInTheDocument()
  })

  it('英文轮次的检查器照常摊开路由决策', () => {
    const { container } = render(<RoutingInspector message={enOrder} />)
    expect(container.textContent).toContain('SO-2026-0912')
    expect(container.querySelectorAll('.meter').length).toBeGreaterThan(0)
  })
})

describe('录制的英文 /conversations/{conv_id} 恢复', () => {
  const conv = fixtureJson<ConversationResponse>('en-conversation.json')
  const restored = restoreMessages(conv.messages)

  it('英文历史同样按 seq 复原并带作答 Agent', () => {
    expect(restored.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant'])
    expect(restored.filter((m) => m.role === 'assistant').map((m) => m.agentType))
      .toEqual(['billing', 'order'])
  })

  it('恢复出来的英文助手消息里没有混进中文固定文案', () => {
    // 转人工通知、Agent 失败兜底这些以前是写死中文的，会直接落进英文正文
    for (const m of restored.filter((x) => x.role === 'assistant')) {
      expect(/[一-鿿]/.test(m.content)).toBe(false)
    }
  })
})
