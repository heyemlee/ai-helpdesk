/**
 * 问数页上的一轮问答。界面状态（哪个折叠打开、表格翻到第几页、看图还是看表、反馈草稿）不放这里，
 * 放在各自组件的 state 里：React 只要 key 不变就留着它们，切语言、切学习模式、回填耗时都不会把
 * 用户展开的东西收回去。
 */
import type { StreamStage } from '@/api/sql/client'
import type { QueryResult } from '@/lib/sql/model'

export type TurnStatus = 'pending' | 'done' | 'error' | 'stopped'
export type Transport = 'stream' | 'query'
/**
 * 停止时显式请服务端取消的结果：请求还在路上（sent）、已受理（accepted）、服务端已经跑完（finished，409）、
 * 取消请求本身失败（failed）。空串是没发过（/query 没有运行号）。
 */
export type CancelState = '' | 'sent' | 'accepted' | 'finished' | 'failed'

/** 服务端记账的耗时、token 与检索命中的表：不在问答的响应里，查完从 /history 取回 */
export interface Backfill {
  serverMs: number | null
  totalTokens: number | null
  candidateTables: string[]
}

export interface Turn {
  id: string
  question: string
  askedAt: string
  status: TurnStatus
  result: QueryResult | null
  error: unknown
  transport: Transport
  stages: StreamStage[]
  stageNow: StreamStage | null
  /** performance.now() 的起点：等待时的秒数、往返耗时 */
  startedAt: number
  /** 服务端这次 run 的编号：A2A 的 taskId（流的首帧），或 /query 的 X-Run-Id */
  runId: string
  cancel: CancelState
  /** 从查询历史"再问一次"来的：这是一次新的查询，旧结果和新结果要分开说 */
  rerun: boolean
  backfill: Backfill | null
}

export const newTurnId = (): string =>
  `t${(globalThis.crypto?.randomUUID?.() ?? Math.random().toString(16).slice(2)).replace(/-/g, '').slice(0, 10)}`

/** 每次打开页面一个新会话；"新对话"、"再问一次"也换一个——服务端按 session 记对话上下文 */
export const newSessionId = (): string =>
  `s-${(globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`)
    .replace(/-/g, '').slice(0, 8)}`
