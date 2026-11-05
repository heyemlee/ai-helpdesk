/**
 * 别的页面把一个问题交给问数页："再问一次"（查询历史、一次查询的经过）、从数据浏览带着表名去问。
 *
 * 只在内存里交接一次：问数页打开时取走，取走就没了。不进地址栏（问题原文不该出现在浏览器历史里），
 * 也不进 localStorage（问题里可能有客户名、单号）。
 */
export interface AskDraft {
  question: string
  /** 交过来的理由，问数页显示在输入框上方（比如"这是 11 月 3 日问过的问题，会读今天的数据"） */
  note?: string
  /** true：直接发出去；false：只填进输入框 */
  send?: boolean
}

let pending: AskDraft | null = null
const listeners = new Set<() => void>()

export function setAskDraft(draft: AskDraft): void {
  pending = draft
  for (const listener of listeners) listener()
}

export function takeAskDraft(): AskDraft | null {
  const draft = pending
  pending = null
  return draft
}

/** 问数页已经开着（只是藏起来了）时，交接要能叫醒它 */
export function onAskDraft(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
