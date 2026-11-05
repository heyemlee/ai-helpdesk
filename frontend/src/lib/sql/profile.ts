/**
 * 表概况（行数、取值分布、按月的记录数）的取数与缓存（移植自旧控制台的 profile.js，缓存交给 TanStack Query）。
 *
 * 概况从 /catalog/{库}/{表}/profile 取，是服务端按当前身份算出来的聚合结果，页面只管画。
 * - 同一张表、同一组条件只取一次（同一个 query key）。只要行数、而带分布的那一份已经有了（或正在取）时，
 *   直接用它，不另发请求。
 * - 目录一刷新，这里的东西就全部作废：key 里带着目录的读取时刻（generated_at），新目录就是新 key，
 *   旧的那批没人用了，由 Query 回收——它们是按上一份目录、上一个身份算的。
 * - 目录页一打开要给每张表各取一次行数，所以取数排队走，同时最多 MAX_PARALLEL 个：一口气发几十个请求，
 *   先被限流的是用户自己接下来要点的那一下。
 */
import {
  keepPreviousData, useQueries, useQuery, useQueryClient, type QueryClient, type QueryKey,
} from '@tanstack/react-query'
import { sqlApi } from '@/api/sql/client'
import type { Catalog, CellValue, ServiceInfo, TableProfile } from '@/api/sql/types'
import type { ViewFilter } from './dataView'

const MAX_PARALLEL = 4

/** 服务端的字段统计里还会带最小、最大值和合计（types.ts 只写了 name / filled） */
export interface ProfileColumnStats {
  name: string
  filled: number
  min?: string | number
  max?: string | number
  sum?: number
}

/** 一个取值；被截过的（太长，服务端只给了开头）拿去做"等于"筛选一条也匹配不上 */
export interface FacetValue {
  value: CellValue
  count: number
  truncated?: boolean
}

export const statsOf = (profile: TableProfile): ProfileColumnStats[] => profile.columns as ProfileColumnStats[]
export const facetValues = (values: TableProfile['facets'][number]['values']): FacetValue[] => values as FacetValue[]

export type ProfileSupport = 'on' | 'off' | 'unsupported' | 'unknown'

/** 这台服务给不给概况：on | off（管理员关了）| unsupported（没有这组接口的旧版本进程）| unknown（还没问到） */
export function profileSupport(service: ServiceInfo | null | undefined): ProfileSupport {
  if (!service) return 'unknown'
  const info = service.profile
  if (!info) return 'unsupported'
  return info.enabled ? 'on' : 'off'
}

export interface ProfileScope {
  search?: string
  filters?: ViewFilter[]
  /** false：只要行数与各字段的填充数 */
  breakdowns?: boolean
}

const normalize = ({ search = '', filters = [], breakdowns = true }: ProfileScope = {}) =>
  ({ search, filters, breakdowns })

export function profileKey(catalog: Catalog, database: string, table: string, scope: ProfileScope = {}): QueryKey {
  const { search, filters, breakdowns } = normalize(scope)
  return ['sql', 'profile', catalog.generated_at, database, table, search,
    filters.map((filter) => [filter.column, filter.op, String(filter.value ?? '')]), breakdowns ? 1 : 0]
}

/**
 * 实际要用的那一份：只要行数、而带分布的那一份已经在缓存里（取到了或者正在取）——它里面就有行数。
 * 出过错的那份不算，那时候还是单独去数。
 */
export function resolveScope(client: QueryClient, catalog: Catalog, database: string, table: string,
  scope: ProfileScope = {}): { key: QueryKey; scope: Required<ProfileScope> } {
  const wanted = normalize(scope)
  if (!wanted.breakdowns) {
    const full = { ...wanted, breakdowns: true }
    const state = client.getQueryState(profileKey(catalog, database, table, full))
    if (state && state.status !== 'error') return { key: profileKey(catalog, database, table, full), scope: full }
  }
  return { key: profileKey(catalog, database, table, wanted), scope: wanted }
}

// ── 排队：同时最多 MAX_PARALLEL 个请求 ──────────────────────────────────────

let running = 0
const waiting: Array<() => void> = []

function acquire(): Promise<void> {
  if (running < MAX_PARALLEL) {
    running += 1
    return Promise.resolve()
  }
  return new Promise((resolve) => waiting.push(resolve))
}

function release(): void {
  const next = waiting.shift()
  // 把名额直接交给下一个：先减再让它加的话，中间插进来的新请求会多占一个
  if (next) next()
  else running -= 1
}

async function queued<T>(task: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  await acquire()
  try {
    // 排队期间用不着了（离开了这一页、目录换了）：不再发
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    return await task()
  } finally {
    release()
  }
}

function fetchProfile(database: string, table: string, scope: Required<ProfileScope>, signal?: AbortSignal) {
  return queued(() => sqlApi.profile(database, table, {
    search: scope.search,
    filters: scope.filters,
    breakdowns: scope.breakdowns,
  }, signal), signal)
}

const enabledFor = (catalog: Catalog | null | undefined) =>
  !!catalog && profileSupport(catalog.service) === 'on'

/**
 * 一张表在一组条件下的概况。服务不给概况、目录还没到时不发请求（enabled=false）。
 * 结果一直有效到目录刷新（staleTime 无限：key 里已经带着目录的读取时刻）。
 * keepPrevious：换了条件、新的还在算时先留着上一份（页面不闪成空白），isPlaceholderData 说明这一点。
 */
export function useTableProfile(catalog: Catalog | null | undefined, database: string, table: string,
  scope: ProfileScope = {}, enabled = true, keepPrevious = false) {
  const client = useQueryClient()
  const resolved = catalog ? resolveScope(client, catalog, database, table, scope) : null
  return useQuery({
    queryKey: resolved?.key ?? ['sql', 'profile', 'none', database, table],
    queryFn: ({ signal }) => fetchProfile(database, table, resolved!.scope, signal),
    enabled: enabled && enabledFor(catalog) && !!resolved && !!database && !!table,
    staleTime: Infinity,
    retry: false,
    ...(keepPrevious ? { placeholderData: keepPreviousData } : {}),
  })
}

export interface TablePair {
  database: string
  table: string
}

/** 一批表各一份概况（目录页的行数、库页面上每张表的分布）。返回值和 pairs 一一对应 */
export function useTableProfiles(catalog: Catalog | null | undefined, pairs: TablePair[], scope: ProfileScope = {}) {
  const client = useQueryClient()
  return useQueries({
    queries: pairs.map(({ database, table }) => {
      const resolved = catalog ? resolveScope(client, catalog, database, table, scope) : null
      return {
        queryKey: resolved?.key ?? ['sql', 'profile', 'none', database, table],
        queryFn: ({ signal }: { signal: AbortSignal }) => fetchProfile(database, table, resolved!.scope, signal),
        enabled: enabledFor(catalog) && !!resolved,
        staleTime: Infinity,
        retry: false,
      }
    }),
  })
}

export interface ProfileEntry {
  status: 'pending' | 'success' | 'error'
  data: TableProfile | undefined
}

/**
 * 已经要过的那一份（可能还在取），没要过就是 null。只看，不发请求：画目录、画一个库的卡片时用这个——
 * 画一遍页面不该顺手发出几十个请求。
 */
export function findProfile(client: QueryClient, catalog: Catalog | null | undefined, database: string, table: string,
  { breakdowns = true }: { breakdowns?: boolean } = {}): ProfileEntry | null {
  if (!catalog) return null
  const read = (full: boolean): ProfileEntry | null => {
    const state = client.getQueryState<TableProfile>(profileKey(catalog, database, table, { breakdowns: full }))
    return state ? { status: state.status, data: state.data } : null
  }
  const full = read(true)
  if (breakdowns) return full
  if (full && full.status !== 'error') return full
  return read(false) ?? full
}

/** 一张表没有筛选时、带分布的那份概况：已经取到就给，没有就是 null。只看，不发请求 */
export function peekProfile(client: QueryClient, catalog: Catalog | null | undefined, database: string, table: string):
  TableProfile | null {
  if (!catalog) return null
  return client.getQueryData<TableProfile>(profileKey(catalog, database, table)) ?? null
}

/** 一张表（没有筛选时）的总行数：number，或者还不知道时是 null。只看已经取到的，不发请求 */
export function knownRowCount(client: QueryClient, catalog: Catalog | null | undefined, database: string, table: string):
  number | null {
  if (!catalog) return null
  for (const breakdowns of [true, false]) {
    const data = client.getQueryData<TableProfile>(profileKey(catalog, database, table, { breakdowns }))
    if (data && typeof data.total_rows === 'number') return data.total_rows
  }
  return null
}
