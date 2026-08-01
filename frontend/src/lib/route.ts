import { useCallback, useEffect, useState } from 'react'

/**
 * 工作台的页面位置，放在 URL 的 hash 里（`#/tickets/tk_123`）。
 *
 * 用 hash 而不是引路由库：只有十来个位置，而"对话里的工单号能点过去"
 * "浏览器后退能回到列表""刷新后还停在这篇文档"这三件事都只需要地址栏里
 * 有个位置。hash 不经过服务器，开发代理、nginx 和后端的静态挂载都不用配。
 */
export type View = 'chat' | 'knowledge' | 'tickets' | 'admin'
export type AdminTab = 'overview' | 'sops' | 'monitor' | 'eval' | 'inspector'
export type KnowledgeMode = 'browse' | 'ask'

export type Route =
  | { view: 'chat' }
  | { view: 'knowledge'; mode: KnowledgeMode; docId?: string }
  | { view: 'tickets'; ticketId?: string }
  | { view: 'admin'; tab: AdminTab }

export const ADMIN_TABS: AdminTab[] = ['overview', 'sops', 'monitor', 'eval', 'inspector']

export function parseRoute(hash: string): Route {
  const [head, second, third] = hash.replace(/^#\/?/, '').split('/').map((p) => {
    try { return decodeURIComponent(p) } catch { return p }
  })
  if (head === 'knowledge') {
    if (second === 'doc' && third) return { view: 'knowledge', mode: 'browse', docId: third }
    return { view: 'knowledge', mode: second === 'ask' ? 'ask' : 'browse' }
  }
  if (head === 'tickets') return second ? { view: 'tickets', ticketId: second } : { view: 'tickets' }
  if (head === 'admin') {
    const tab = ADMIN_TABS.find((t) => t === second) ?? 'overview'
    return { view: 'admin', tab }
  }
  return { view: 'chat' }
}

/** 各个位置的链接。组件里一律用这里的函数拼，不手写 hash 字符串。 */
export const href = {
  chat: () => '#/chat',
  knowledge: (mode: KnowledgeMode = 'browse') => (mode === 'ask' ? '#/knowledge/ask' : '#/knowledge'),
  document: (docId: string) => `#/knowledge/doc/${encodeURIComponent(docId)}`,
  tickets: () => '#/tickets',
  ticket: (ticketId: string) => `#/tickets/${encodeURIComponent(ticketId)}`,
  admin: (tab: AdminTab = 'overview') => (tab === 'overview' ? '#/admin' : `#/admin/${tab}`),
}

export function useRoute(): { route: Route; navigate: (to: string) => void } {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))

  useEffect(() => {
    const sync = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', sync)
    return () => window.removeEventListener('hashchange', sync)
  }, [])

  const navigate = useCallback((to: string) => {
    if (window.location.hash === to) return
    window.location.hash = to
    // jsdom 和个别内嵌浏览器不在同一个事件循环里派发 hashchange，这里直接同步一次
    setRoute(parseRoute(to))
  }, [])

  return { route, navigate }
}
