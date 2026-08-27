import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, streamChat, type Settings } from '@/api/client'
import type {
  ChatResponse, ConversationEntry, RoutingConfig, StreamEvent, StreamMetaEvent, StreamProgressEvent,
  StreamRoutingEvent, Ticket, TurnProjection,
} from '@/api/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** 正在接收流式增量 */
  streaming?: boolean
  /** 请求失败时的错误文案 */
  error?: string
  /** 失败时的 HTTP 状态码；连不上服务端记 0，流中途报错没有状态码 */
  errorStatus?: number
  /** 准备阶段的进度，按到达顺序累积；第一个字吐出来之前它就是全部可见反馈 */
  progress?: StreamProgressEvent[]
  meta?: Partial<StreamMetaEvent>
  routing?: StreamRoutingEvent
  result?: Partial<ChatResponse>
  /**
   * 刷新后从服务端审计日志恢复的历史消息：只有原文和作答 Agent。当轮的路由决策要按
   * turnId 从事件日志取回（检查器、学习模式打开时才取），见 withTurn
   */
  restored?: boolean
  /** 恢复的助手消息实际作答的 Agent（后端 agent_type 字段） */
  agentType?: string
  /** 恢复的助手消息那一轮建出的工单（按 turn_id，老记录按 request_id 从工单列表对上的） */
  ticketId?: string
  /** 恢复的助手消息是哪一轮：按它取回这一轮的经过。老记录、老后端没有 */
  turnId?: string
}

const newId = () =>
  (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`)

/**
 * 把一条 SSE 事件叠到助手消息上。抽成纯函数是为了让测试能用真后端录下来的
 * 事件流原样回放——手写的 meta/routing 对象永远长不成真后端的样子。
 */
export function applyStreamEvent(m: ChatMessage, evt: StreamEvent): ChatMessage {
  switch (evt.event) {
    case 'progress': return { ...m, progress: [...(m.progress ?? []), evt.data] }
    case 'meta':    return { ...m, meta: evt.data }
    case 'routing': return { ...m, routing: evt.data }
    case 'delta':   return { ...m, content: m.content + evt.data.text }
    case 'result':  return { ...m, result: evt.data, content: evt.data.response || m.content }
    case 'error':   return { ...m, error: evt.data.detail }
  }
}

/**
 * 非流式 /chat 的响应 → 助手消息。
 *
 * 流式那边路由决策是单独一个 routing 事件；非流式的响应体里同样有主辅 Agent，
 * 这里补成同一个形状。**不补的话回复永远显示不出来**：气泡靠 routing 判断
 * "已经知道是谁在答了"，没有它就一直停在"正在处理"——关掉流式之后每一条
 * 回复都是这样，而请求其实早就成功返回了。
 */
export function applyChatResponse(m: ChatMessage, res: ChatResponse): ChatMessage {
  return {
    ...m,
    content: res.response,
    result: res,
    meta: res,
    routing: {
      primary_agent: res.primary_agent || res.agent_type,
      supporting_agents: res.supporting_agents ?? [],
      reason: res.routing_reason ?? '',
      confidence: res.routing_confidence ?? 0,
    },
  }
}

/**
 * 刷新前那一轮：把后端从事件投影出来的 meta / routing / result 拼回消息上，检查器和
 * 学习模式照当轮的样子渲染，不写第二套显示逻辑。
 *
 * 正文优先用投影里的（和当轮发出去的一致）；原文已按保留期清空时它是空的，留着审计
 * 日志里的那份——审计日志是合规留档，保留期比事件日志长。
 */
export function withTurn(m: ChatMessage, turn: TurnProjection): ChatMessage {
  const routing = turn.routing?.primary_agent ? (turn.routing as StreamRoutingEvent) : undefined
  return {
    ...m,
    restored: false,
    content: turn.result?.response || m.content,
    meta: turn.meta,
    routing,
    result: turn.result,
  }
}

/**
 * 这一轮是不是兜底 Agent 替专家答的。是的话返回实际作答的 Agent，否则 undefined。
 *
 * 只认后端明说的那一种：路由选了某个专家，响应里的 agent_type 却是兜底 Agent，
 * 而且这一轮既没转人工也不是澄清追问。多 Agent 协作里辅助 Agent 不算。
 */
export function fallbackAgent(
  routing: ChatMessage['routing'], result: ChatMessage['result'], config?: Pick<RoutingConfig, 'fallback_agent'>,
): string | undefined {
  const answered = result?.agent_type
  const primary = routing?.primary_agent
  if (!answered || !primary || result?.handoff || result?.needs_clarification) return undefined
  if (answered === primary || primary === 'handoff') return undefined
  if ((routing?.supporting_agents ?? []).includes(answered)) return undefined
  return answered === (config?.fallback_agent ?? 'general') ? answered : undefined
}

/**
 * 把 /conversations/{conv_id} 的审计日志变成对话流里的消息。
 * 按 seq 排序而不是信任返回顺序；只认 user/assistant 两种角色。
 *
 * 审计日志不记工单号。传了工单列表的话，把那一轮建出的工单对回去——刷新之后，转人工
 * 的那条回复下面还能看到工单并点过去。先按 turn_id 对（服务端生成，可靠）；老记录没有
 * turn_id，退回按 request_id 对（它可能由客户端决定）。对不上就不显示，不猜。
 */
export function restoreMessages(entries: ConversationEntry[], tickets: Ticket[] = []): ChatMessage[] {
  const byTurn = new Map(tickets.filter((t) => t.turn_id).map((t) => [t.turn_id!, t.id]))
  const byRequest = new Map(tickets.filter((t) => t.request_id).map((t) => [t.request_id, t.id]))
  return [...entries]
    .sort((a, b) => a.seq - b.seq)
    .filter((e) => e.role === 'user' || e.role === 'assistant')
    .map((e) => e.role === 'user'
      ? { id: `restored-${e.seq}`, role: 'user' as const, content: e.content, restored: true }
      : { id: `restored-${e.seq}`, role: 'assistant' as const, content: e.content,
          restored: true, agentType: e.agent_type || undefined, turnId: e.turn_id || undefined,
          ticketId: (e.turn_id && byTurn.get(e.turn_id))
            || (e.request_id && byRequest.get(e.request_id)) || undefined })
}

/**
 * 对话状态机。流式和非流式共用同一份消息结构，
 * 这样切换开关时 UI 不需要分两套渲染逻辑。
 *
 * onConversationLost：settings 里存着的 conv_id 服务端已经不认（404，超出留存期
 * 或库换了）时回调，由上层把它清掉——否则下一条消息会顶着一个操作员以为有
 * 历史、其实是空的 id 发出去。
 */
export function useChatStream(
  settings: Settings,
  onConversationId: (id: string) => void,
  onConversationLost?: () => void,
) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const messagesRef = useRef(messages)
  messagesRef.current = messages

  const patch = useCallback((id: string, updater: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? updater(m) : m)))
  }, [])

  // 挂载或换会话时从服务端恢复历史。
  //
  // 之前 conv_id 存在 localStorage、每条消息都带着发，但从不拉历史：刷新一下
  // 屏幕空了，服务端记忆还在，下一条消息继续那个会话——模型有操作员看不到的
  // 上下文。实测撞到过：Agent 回了一句「订单号（当前是 SO-2026-0912 吗？）」，
  // 那个单号来自一个已经不在屏幕上的轮次。
  //
  // 本机已经有消息时跳过：要么这个 id 是本页刚拿到的（首条消息的 meta 事件
  // 刚把它写进 settings），要么用户正在聊；两种情况都不该用服务端副本覆盖
  // 手里带着路由决策的那份。想回查另一个会话：先「新建对话」清空，再填 id。
  useEffect(() => {
    const id = settings.conversationId
    if (!id || messagesRef.current.length > 0) return
    let cancelled = false
    setRestoring(true)
    Promise.all([
      api.conversation(settings.apiBaseUrl, id),
      // 工单只是给历史消息补一个链接：取不到（没有钥匙、老后端）不影响恢复对话本身
      api.tickets(settings.apiBaseUrl).then((res) => res.tickets).catch(() => []),
    ])
      .then(([res, tickets]) => {
        if (cancelled) return
        const own = tickets.filter((t) => t.conv_id === id)
        setMessages((prev) => (prev.length ? prev : restoreMessages(res.messages, own)))
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) onConversationLost?.()
        // 其它错误（鉴权、断网）保持空白，不挡新对话；控制台里留一条线索
        else console.warn('恢复会话失败', err)
      })
      .finally(() => { if (!cancelled) setRestoring(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.conversationId, settings.apiBaseUrl])

  const send = useCallback(
    async (text: string, streaming: boolean) => {
      const content = text.trim()
      if (!content || busy) return

      const assistantId = newId()
      setMessages((prev) => [
        ...prev,
        { id: newId(), role: 'user', content },
        { id: assistantId, role: 'assistant', content: '', streaming },
      ])
      setBusy(true)

      const body = {
        message: content,
        user_id: settings.userId || 'anonymous',
        ...(settings.conversationId ? { conv_id: settings.conversationId } : {}),
      }

      try {
        if (streaming) {
          const controller = new AbortController()
          abortRef.current = controller

          for await (const evt of streamChat(settings.apiBaseUrl, body, controller.signal)) {
            if (evt.event === 'meta') onConversationId(evt.data.conv_id)
            if (evt.event === 'error') throw new Error(evt.data.detail)
            patch(assistantId, (m) => applyStreamEvent(m, evt))
          }
        } else {
          const res = await api.chat(settings.apiBaseUrl, body)
          onConversationId(res.conv_id)
          patch(assistantId, (m) => applyChatResponse(m, res))
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        // 状态码留着：401 要告诉员工去哪填钥匙，光一句"401 Unauthorized"不知道下一步做什么
        const errorStatus = err instanceof ApiError ? err.status
          : err instanceof TypeError ? 0 : undefined
        patch(assistantId, (m) => ({ ...m, error: message, errorStatus }))
      } finally {
        abortRef.current = null
        patch(assistantId, (m) => ({ ...m, streaming: false }))
        setBusy(false)
      }
    },
    [busy, settings, onConversationId, patch],
  )

  const stop = useCallback(() => abortRef.current?.abort(), [])
  const clear = useCallback(() => setMessages([]), [])

  return { messages, busy, restoring, send, stop, clear }
}
