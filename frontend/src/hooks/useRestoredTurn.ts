import { ApiError } from '@/api/client'
import { useTurn } from '@/api/hooks'
import type { TurnAbandoned } from '@/api/types'
import { withTurn, type ChatMessage } from './useChatStream'

/**
 * 刷新前那一轮的经过，从事件日志取回（GET /turns/{turn_id}）。
 *
 * - none：没有 turn_id（早于事件日志，或当时日志没开），没什么可取的；
 * - expired：后端 404，超出了保留期，或当时日志没开；
 * - ready：拼回成一条带 meta / routing / result 的消息，原来的检查器和学习模式照样渲染。
 *
 * 用到 TanStack Query：只在有 turn_id 的地方调它，调用方外面要有 QueryClientProvider。
 */
export type RestoredTurn =
  | { state: 'none' }
  | { state: 'loading' }
  | { state: 'expired' }
  | { state: 'error'; error: unknown }
  | { state: 'ready'; message: ChatMessage; redacted: boolean; complete: boolean; abandoned: TurnAbandoned | null }

export function useRestoredTurn(base: string, message: ChatMessage, enabled = true): RestoredTurn {
  const query = useTurn(base, message.turnId, enabled)
  if (!message.turnId) return { state: 'none' }
  if (query.data) {
    return { state: 'ready', message: withTurn(message, query.data),
             redacted: query.data.redacted, complete: query.data.complete,
             abandoned: query.data.abandoned ?? null }
  }
  if (query.isError) {
    return query.error instanceof ApiError && query.error.status === 404
      ? { state: 'expired' } : { state: 'error', error: query.error }
  }
  return { state: 'loading' }
}
