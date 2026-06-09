import { useCallback, useRef, useState } from 'react'
import { api, streamChat, type Settings } from '@/api/client'
import type { ChatResponse, StreamMetaEvent, StreamRoutingEvent } from '@/api/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** 正在接收流式增量 */
  streaming?: boolean
  /** 请求失败时的错误文案 */
  error?: string
  meta?: Partial<StreamMetaEvent>
  routing?: StreamRoutingEvent
  result?: Partial<ChatResponse>
}

const newId = () =>
  (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`)

/**
 * 对话状态机。流式和非流式共用同一份消息结构，
 * 这样切换开关时 UI 不需要分两套渲染逻辑。
 */
export function useChatStream(settings: Settings, onConversationId: (id: string) => void) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  const patch = useCallback((id: string, updater: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? updater(m) : m)))
  }, [])

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
            if (evt.event === 'meta') {
              onConversationId(evt.data.conv_id)
              patch(assistantId, (m) => ({ ...m, meta: evt.data }))
            } else if (evt.event === 'routing') {
              patch(assistantId, (m) => ({ ...m, routing: evt.data }))
            } else if (evt.event === 'delta') {
              patch(assistantId, (m) => ({ ...m, content: m.content + evt.data.text }))
            } else if (evt.event === 'result') {
              patch(assistantId, (m) => ({ ...m, result: evt.data, content: evt.data.response || m.content }))
            } else if (evt.event === 'error') {
              throw new Error(evt.data.detail)
            }
          }
        } else {
          const res = await api.chat(settings.apiBaseUrl, body)
          onConversationId(res.conv_id)
          patch(assistantId, (m) => ({ ...m, content: res.response, result: res, meta: res }))
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        patch(assistantId, (m) => ({ ...m, error: message }))
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

  return { messages, busy, send, stop, clear }
}
