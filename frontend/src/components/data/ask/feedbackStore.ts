/**
 * 已经提交过的反馈：trace_id → 判定。只是这个浏览器里的方便（刷新后还认得"这条已经反馈过了"），
 * 判定本身存在服务端；存不了（隐私模式、被禁用）就只在这一页有效。
 */
import { useSyncExternalStore } from 'react'

export type Verdict = 'correct' | 'wrong' | 'unsure'

const STORAGE_KEY = 'helpdesk.sql.feedback'
const MAX_ENTRIES = 200

function read(): Record<string, Verdict> {
  try {
    const saved = JSON.parse(globalThis.localStorage?.getItem(STORAGE_KEY) ?? '{}') as unknown
    return saved && typeof saved === 'object' ? (saved as Record<string, Verdict>) : {}
  } catch { return {} }
}

let sent: Record<string, Verdict> = read()
const listeners = new Set<() => void>()

function write(next: Record<string, Verdict>) {
  const entries = Object.entries(next)
  sent = entries.length > MAX_ENTRIES ? Object.fromEntries(entries.slice(-MAX_ENTRIES)) : next
  try { globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(sent)) } catch { /* 存不下就只在这一页有效 */ }
  for (const listener of listeners) listener()
}

export function recordFeedback(traceId: string, verdict: Verdict): void {
  write({ ...sent, [traceId]: verdict })
}

export function forgetFeedback(traceId: string): void {
  const { [traceId]: _dropped, ...rest } = sent
  void _dropped
  write(rest)
}

const subscribe = (listener: () => void) => {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** 这一条反馈过没有、反馈的是什么 */
export function useSentFeedback(traceId: string): Verdict | undefined {
  return useSyncExternalStore(subscribe, () => sent[traceId], () => undefined)
}
