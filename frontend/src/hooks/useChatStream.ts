import type { PolicyScope } from '@/api/types'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, caseApi, streamChat, streamRun, type Settings } from '@/api/client'
import type {
  Attachment, CaseInput, ChatRequest, ChatResponse, ConversationEntry, DegradeInfo, RoutingConfig, StreamEvent, StreamMetaEvent,
  StreamProgressEvent, StreamRoutingEvent, Ticket, TurnProjection,
} from '@/api/types'

export interface ChatMessage {
  attachments?: { id: string; filename?: string }[]
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
  /** 排队模式：这一轮的 run。断线之后按它续流 */
  runId?: string
  /** 排队模式：第几次执行。大于 1 说明处理它的 worker 中途失联，换了一个从头再来 */
  attempt?: number
  /** 排队模式：这一轮按哪一档降级（attempt 帧里先到，meta / result 里还会再带一次） */
  degrade?: DegradeInfo
  /** 连接断了，正在按 run 续流 */
  reconnecting?: boolean
}
export interface ChatMaterials { policyScope?: PolicyScope; attachments: Attachment[]; caseChoice: string; issueType: CaseInput['issue_type'] }

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
    case 'result':  return evt.data.case_id ? applyChatResponse(m, evt.data as ChatResponse)
      : { ...m, result: evt.data, content: evt.data.response || m.content }
    case 'error':   return { ...m, error: evt.data.detail }
    case 'run':     return { ...m, runId: evt.data.run_id }
    // 换人重跑（后端 app/runs/worker.py）：上一遍已经发出来的半截作废，从头再收。
    // 第 1 次执行只记下档位，什么都不清
    case 'attempt':
      return evt.data.attempt > 1
        ? { ...m, attempt: evt.data.attempt, degrade: evt.data.degrade, content: '', progress: [],
            meta: undefined, routing: undefined, result: undefined, error: undefined }
        : { ...m, attempt: evt.data.attempt, degrade: evt.data.degrade }
  }
}

/** 续流最多试几次、每次等多久（毫秒）：网络抖一下通常一两秒就回来了 */
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 8000]

/** 后端在流里报的错（error 事件）：这一轮的结局，不是断线，不续流 */
class StreamFailure extends Error {}

/**
 * 续流时值得再试的 HTTP 状态：API 实例正在重启、发版，前面的代理（nginx、Vite）先回了网关错误。
 * 404（run 不在了）、401（钥匙不对）这类再试也一样，照原样报出来。
 */
const RETRYABLE_STATUS = new Set([502, 503, 504])
const retryable = (err: unknown) =>
  !(err instanceof StreamFailure) && (!(err instanceof ApiError) || RETRYABLE_STATUS.has(err.status))

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  const timer = setTimeout(resolve, ms)
  signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
})

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
  onCaseId?: (id: string) => void,
) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const messagesRef = useRef(messages)
  const pendingCase = useRef<{ fingerprint: string; originalFingerprint: string; body: ChatRequest;
    assistantId: string; resumeRun?: string } | null>(null)
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
    if (settings.caseId) {
      if (messagesRef.current.length) return
      let cancelled = false
      let timer: ReturnType<typeof setTimeout> | undefined
      setRestoring(true)
      const restoreCase = async () => {
        try {
          const c = await caseApi.get(settings.apiBaseUrl, settings.caseId!)
          if (cancelled) return
          setMessages(c.messages.filter(m => m.role === 'user' || m.role === 'assistant').map(m => ({
            id: m.message_id, role: m.role as 'user' | 'assistant', content: m.content, restored: true,
            ticketId: c.id, attachments: (m.attachment_ids || []).map(id => ({ id,
              filename: c.materials.find(a => a.attachment_id === id)?.attachment?.filename })),
          })))
          const running = c.runs.some(r => r.status === 'queued' || r.status === 'running')
          setRestoring(running)
          if (running) timer = setTimeout(restoreCase, 800)
          else onConversationId(c.conv_id)
        } catch {
          if (!cancelled) {
            setRestoring(false)
            setMessages([{ id: 'case-access', role: 'assistant', content: '', error: '无法读取工单，请检查员工登录后重试。' }])
          }
        }
      }
      void restoreCase()
      return () => { cancelled = true; clearTimeout(timer); setRestoring(false) }
    }
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
  }, [settings.conversationId, settings.apiBaseUrl, settings.caseId])

  const send = useCallback(
    async (text: string, streaming: boolean, materials?: ChatMaterials) => {
      const content = text.trim()
      if ((!content && !materials?.attachments.length) || busy) return false

      const choice = materials?.caseChoice || settings.caseId
      const fingerprint = JSON.stringify({ content, ids: materials?.attachments.map(a => a.id), choice, issue: materials?.issueType, policyScope: materials?.policyScope })
      const pending = pendingCase.current
      const retrying = pending && [pending.fingerprint, pending.originalFingerprint].includes(fingerprint) ? pending : null
      const assistantId = retrying?.assistantId || newId()
      if (retrying) patch(assistantId, m => ({ ...m, error: undefined, errorStatus: undefined, streaming: true }))
      else setMessages((prev) => [
        ...prev,
        { id: newId(), role: 'user', content, attachments: materials?.attachments },
        { id: assistantId, role: 'assistant', content: '', streaming },
      ])
      setBusy(true)

      let body: ChatRequest = {
        message: content,
        ...(materials?.policyScope ? { policy_scope: materials.policyScope } : {}),
        user_id: settings.userId || 'anonymous',
        ...(settings.conversationId ? { conv_id: settings.conversationId } : {}),
      }

      try {
        if (materials?.attachments.length && !choice) throw new Error('请明确选择已有问题或新建问题。')
        if (choice) {
          if (retrying) body = retrying.body
          else {
            const current = choice === 'new' ? null : await caseApi.get(settings.apiBaseUrl, choice)
            body = { ...body, message_id: newId(), conv_id: current?.conv_id || settings.conversationId || (await api.newConversation(settings.apiBaseUrl)).conv_id,
              attachment_ids: materials?.attachments.map(a => a.id) || [],
              ...(current ? { case_id: current.id, expected_version: current.version, expected_control_version: current.control_version }
                : { new_case: true, issue_type: materials?.issueType || 'general' }) }
            pendingCase.current = { fingerprint, originalFingerprint: fingerprint, body, assistantId }
          }
          streaming = true
        }
        if (streaming) {
          const controller = new AbortController()
          abortRef.current = controller

          // 排队模式下流断了（网络抖动、代理超时、API 实例在发版）这一轮并没有停：
          // 带着最后收到的那一帧的 id 去 /runs/{run_id}/events 接着读。inline 模式没有
          // run 帧，断了就是断了，和原来一样
          let runId = retrying?.resumeRun
          let lastEventId: string | undefined
          let finished = false
          let nextRun: string | undefined
          let continuations = 0
          let stream = runId ? streamRun(settings.apiBaseUrl, runId, undefined, controller.signal)
            : streamChat(settings.apiBaseUrl, body, controller.signal, body.message_id || newId())
          for (let retry = 0; ; retry += 1) {
            try {
              for await (const evt of stream) {
                if (controller.signal.aborted) break
                if (evt.id) lastEventId = evt.id
                if (evt.event === 'run') {
                  runId = evt.data.run_id
                  if (pendingCase.current) pendingCase.current.resumeRun = runId
                  if (evt.data.case_id) {
                    if (pendingCase.current) pendingCase.current.fingerprint = JSON.stringify({ content,
                      ids: materials?.attachments.map(a => a.id), choice: evt.data.case_id, issue: materials?.issueType, policyScope: materials?.policyScope })
                    onCaseId?.(evt.data.case_id); onConversationId(evt.data.conv_id)
                  }
                }
                if (evt.event === 'meta') onConversationId(evt.data.conv_id)
                if (evt.event === 'error') throw new StreamFailure(evt.data.detail)
                patch(assistantId, (m) => ({ ...applyStreamEvent(m, evt), reconnecting: false }))
                if (evt.event === 'result') {
                  finished = true; nextRun = evt.data.next_run_id
                  if (evt.data.case_id) {
                    if (pendingCase.current) pendingCase.current.fingerprint = JSON.stringify({ content,
                      ids: materials?.attachments.map(a => a.id), choice: evt.data.case_id, issue: materials?.issueType, policyScope: materials?.policyScope })
                    onCaseId?.(evt.data.case_id); onConversationId(evt.data.conv_id)
                  }
                }
              }
            } catch (err) {
              if (!retryable(err) || controller.signal.aborted || !runId) throw err
            }
            if (finished && nextRun && !controller.signal.aborted) {
              if (++continuations > 1) throw new Error('Unexpected material continuation')
              runId = nextRun; nextRun = undefined; finished = false; lastEventId = undefined
              if (pendingCase.current) pendingCase.current.resumeRun = runId
              stream = streamRun(settings.apiBaseUrl, runId, undefined, controller.signal)
              continue
            }
            if (finished || !runId || controller.signal.aborted) break
            const delay = RECONNECT_DELAYS_MS[retry]
            if (delay === undefined) throw new Error('与服务端的连接断开，重连几次都没有成功')
            patch(assistantId, (m) => ({ ...m, reconnecting: true }))
            await sleep(delay, controller.signal)
            stream = streamRun(settings.apiBaseUrl, runId, lastEventId, controller.signal)
          }
          if (controller.signal.aborted) return false
          if (!finished) throw new Error('未收到完成回执，输入已保留，请重试。')
        } else {
          const res = await api.chat(settings.apiBaseUrl, body)
          onConversationId(res.conv_id)
          patch(assistantId, (m) => applyChatResponse(m, res))
        }
        pendingCase.current = null
        return true
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) pendingCase.current = null
        const message = err instanceof Error ? err.message : String(err)
        // 状态码留着：401 要告诉员工去哪填钥匙，光一句"401 Unauthorized"不知道下一步做什么
        const errorStatus = err instanceof ApiError ? err.status
          : err instanceof TypeError ? 0 : undefined
        patch(assistantId, (m) => ({ ...m, error: message, errorStatus }))
        return false
      } finally {
        abortRef.current = null
        patch(assistantId, (m) => ({ ...m, streaming: false, reconnecting: false }))
        setBusy(false)
      }
    },
    [busy, settings, onConversationId, onCaseId, patch],
  )

  const stop = useCallback(() => abortRef.current?.abort(), [])
  const clear = useCallback(() => { abortRef.current?.abort(); pendingCase.current = null; setMessages([]) }, [])

  return { messages, busy, restoring, send, stop, clear }
}
