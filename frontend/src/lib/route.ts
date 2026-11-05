import { useCallback, useEffect, useState } from 'react'

/**
 * 工作台的页面位置，放在 URL 的 hash 里（`#/tickets/tk_123`）。
 *
 * 用 hash 而不是引路由库：只有十来个位置，而"对话里的工单号能点过去"
 * "浏览器后退能回到列表""刷新后还停在这篇文档"这三件事都只需要地址栏里
 * 有个位置。hash 不经过服务器，开发代理、nginx 和后端的静态挂载都不用配。
 */
export type View = 'chat' | 'knowledge' | 'tickets' | 'data' | 'admin'
export type AdminTab = 'overview' | 'models' | 'sql' | 'sops' | 'monitor' | 'eval' | 'inspector'
export type KnowledgeMode = 'browse' | 'ask'
/** 数据（SQL Agent）的几页：问数、数据浏览、查询历史，以及一次查询的经过 */
export type DataPage = 'ask' | 'explore' | 'history' | 'run'
export const DATA_TABS: Exclude<DataPage, 'run'>[] = ['ask', 'explore', 'history']

export type Route =
  | { view: 'chat' }
  | { view: 'knowledge'; mode: KnowledgeMode; docId?: string; category?: string }
  | { view: 'tickets'; ticketId?: string }
  | DataRoute
  | { view: 'admin'; tab: AdminTab }

/**
 * `#/data/explore/<库>/<表>?q=…&f=…` 里问号后面那段是表页自己的状态（搜索、筛选、排序、页码），
 * 由数据浏览页自己解析（lib/dataView.ts），这里原样带过去。它不参与分段：表名里真有问号的话，
 * 链接里是转义过的。
 */
export type DataRoute =
  | { view: 'data'; page: 'ask' }
  | { view: 'data'; page: 'explore'; database?: string; table?: string; query: string }
  | { view: 'data'; page: 'history' }
  | { view: 'data'; page: 'run'; runId: string }

export const ADMIN_TABS: AdminTab[] = ['overview', 'models', 'sql', 'sops', 'monitor', 'eval', 'inspector']

export function parseRoute(hash: string): Route {
  const mark = hash.indexOf('?')
  const path = mark === -1 ? hash : hash.slice(0, mark)
  const query = mark === -1 ? '' : hash.slice(mark + 1)
  const [head, second, third, fourth] = path.replace(/^#\/?/, '').split('/').map((p) => {
    try { return decodeURIComponent(p) } catch { return p }
  })
  if (head === 'data') return parseDataRoute(second, third, fourth, query)
  if (head === 'knowledge') {
    if (second === 'doc' && third) return { view: 'knowledge', mode: 'browse', docId: third }
    if (second === 'category' && third) return { view: 'knowledge', mode: 'browse', category: third }
    return { view: 'knowledge', mode: second === 'ask' ? 'ask' : 'browse' }
  }
  if (head === 'tickets') return second ? { view: 'tickets', ticketId: second } : { view: 'tickets' }
  if (head === 'admin') {
    const tab = ADMIN_TABS.find((t) => t === second) ?? 'overview'
    return { view: 'admin', tab }
  }
  return { view: 'chat' }
}

function parseDataRoute(page = '', first = '', second = '', query = ''): DataRoute {
  if (page === 'explore') {
    return { view: 'data', page: 'explore', ...(first ? { database: first } : {}),
             ...(first && second ? { table: second } : {}), query }
  }
  if (page === 'history') return { view: 'data', page: 'history' }
  if (page === 'runs' && first) return { view: 'data', page: 'run', runId: first }
  return { view: 'data', page: 'ask' }
}

const seg = encodeURIComponent

/** 各个位置的链接。组件里一律用这里的函数拼，不手写 hash 字符串。 */
export const href = {
  chat: () => '#/chat',
  knowledge: (mode: KnowledgeMode = 'browse') => (mode === 'ask' ? '#/knowledge/ask' : '#/knowledge'),
  document: (docId: string) => `#/knowledge/doc/${encodeURIComponent(docId)}`,
  /** 浏览某一个分类；`uncategorized` 是未分类 */
  category: (key: string) => `#/knowledge/category/${encodeURIComponent(key)}`,
  tickets: () => '#/tickets',
  ticket: (ticketId: string) => `#/tickets/${encodeURIComponent(ticketId)}`,
  admin: (tab: AdminTab = 'overview') => (tab === 'overview' ? '#/admin' : `#/admin/${tab}`),
  dataAsk: () => '#/data/ask',
  dataHistory: () => '#/data/history',
  /** 数据浏览：不给库是目录，给了库是这个库的表，再给表是那张表；query 是表页的状态（不带问号） */
  dataExplore: (database = '', table = '', query = '') => {
    let to = '#/data/explore'
    if (database) to += `/${seg(database)}`
    if (database && table) to += `/${seg(table)}`
    return query ? `${to}?${query}` : to
  },
  /** 一次查询（SQL Agent 的 run）的经过。run_id 就是 A2A 的 taskId、/query 的 X-Run-Id */
  dataRun: (runId: string) => `#/data/runs/${seg(runId)}`,
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
