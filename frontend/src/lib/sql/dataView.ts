/**
 * 一张表"正在怎么看"写进地址里（移植自旧控制台的 dataRoute.js）：搜索词、筛选、排序、页码、标签页。
 *
 *   #/data/explore/<库>/<表>?q=…&f=<字段>:<条件>:<值>&sort=<字段>.desc&page=2&size=50&tab=fields
 *
 * 这样刷新不丢筛选，"把我正在看的这批记录发给同事"发的就是这个地址，从一条记录跳到另一张表里的
 * 对应记录也只是一个普通链接。地址里 # 后面的部分不会发给服务端；真正读数据时条件走请求体，
 * 由服务端按清单核对。
 *
 * 筛选条件的三段各自先转义再用冒号连起来（转义之后段内不会再有冒号），所以字段名、筛选值里有冒号、
 * 空格、中文都不影响拆分。
 */
import type { PreviewOperator } from '@/api/sql/types'
import { href } from '@/lib/route'

export const PAGE_SIZES = [25, 50, 100] as const
export const OPERATORS: readonly PreviewOperator[] = [
  'eq', 'ne', 'contains', 'starts_with', 'gt', 'gte', 'lt', 'lte', 'is_empty', 'not_empty',
]
/** 不用填值的条件 */
export const VALUELESS: readonly PreviewOperator[] = ['is_empty', 'not_empty']
const TABS = ['records', 'fields'] as const
export const MAX_FILTERS = 5

export type TableTab = (typeof TABS)[number]

export interface ViewFilter {
  column: string
  op: PreviewOperator
  value: string
}

export interface ViewSort {
  key: string
  direction: 'asc' | 'desc'
}

export interface TableView {
  tab: TableTab
  search: string
  filters: ViewFilter[]
  sort: ViewSort | null
  page: number
  pageSize: number
}

export const freshView = (): TableView => ({
  tab: 'records', search: '', filters: [], sort: null, page: 1, pageSize: PAGE_SIZES[0],
})

const decode = (text: string): string | null => {
  try { return decodeURIComponent(text) } catch { return null }
}

/** 查询串 → 视图状态。认不出来的部分直接丢掉，不报错：地址是人会手改、会被截断的东西。 */
export function parseView(queryString = ''): TableView {
  const view = freshView()
  const params = new URLSearchParams(queryString)

  view.search = (params.get('q') ?? '').slice(0, 100)
  for (const item of params.getAll('f').slice(0, MAX_FILTERS)) {
    const parts = item.split(':')
    if (parts.length !== 3) continue
    const column = decode(parts[0]!)
    const op = parts[1] as PreviewOperator
    const value = decode(parts[2]!)
    if (!column || value === null || !OPERATORS.includes(op)) continue
    view.filters.push({ column, op, value })
  }

  const match = /^(.*)\.(asc|desc)$/.exec(params.get('sort') ?? '')
  if (match && match[1]) view.sort = { key: match[1], direction: match[2] as 'asc' | 'desc' }

  const page = Number(params.get('page'))
  if (Number.isInteger(page) && page >= 1) view.page = page
  const size = Number(params.get('size'))
  if ((PAGE_SIZES as readonly number[]).includes(size)) view.pageSize = size
  const tab = params.get('tab')
  if ((TABS as readonly string[]).includes(tab ?? '')) view.tab = tab as TableTab
  return view
}

/** 视图状态 → 查询串（不带问号）。和默认值一样的不写，地址尽量短。 */
export function formatView(view: Partial<TableView>): string {
  const params = new URLSearchParams()
  if (view.search) params.set('q', view.search)
  for (const filter of view.filters ?? []) {
    params.append('f', [encodeURIComponent(filter.column), filter.op, encodeURIComponent(filter.value ?? '')].join(':'))
  }
  if (view.sort) params.set('sort', `${view.sort.key}.${view.sort.direction}`)
  if (view.page && view.page > 1) params.set('page', String(view.page))
  if (view.pageSize && view.pageSize !== PAGE_SIZES[0]) params.set('size', String(view.pageSize))
  if (view.tab && view.tab !== TABS[0]) params.set('tab', view.tab)
  return params.toString()
}

/** 数据浏览里的地址。库、表、视图都是可选的；给了视图就把它写进查询串 */
export function exploreHref(database = '', table = '', view: Partial<TableView> | null = null): string {
  const query = view ? formatView({ ...freshView(), ...view }) : ''
  return href.dataExplore(database, database ? table : '', query)
}

/**
 * 地址跟着视图走，但不产生新的历史记录（翻页、加筛选不该让"后退"一格一格往回退）：
 * 换掉当前这条记录，再告诉路由地址变了——replaceState 自己不触发 hashchange。
 */
export function replaceHash(to: string): void {
  if (window.location.hash === to) return
  window.history.replaceState(window.history.state, '', to)
  window.dispatchEvent(new Event('hashchange'))
}
