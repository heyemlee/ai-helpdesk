import { useQuery } from '@tanstack/react-query'
import { sqlApi } from './client'

/**
 * SQL Agent 的只读数据，和工作台其余部分一样走 TanStack Query。
 *
 * 目录（/catalog）带着本次请求的身份和服务能力一起回来；目录拿不到（浏览被关掉、被限流、出错）时
 * 身份另从 /whoami 取——能不能浏览数据和能不能提问是两回事，后者不该被前者连累。
 * 后发的请求说了算由 Query 保证：同一个 key 晚回来的旧响应不会盖掉新的。
 */

export function useSqlCatalog() {
  return useQuery({
    queryKey: ['sql', 'catalog'],
    queryFn: () => sqlApi.catalog(),
    // 目录是语义层和表结构，几分钟变一次已经算勤；页面上有"刷新"
    staleTime: 5 * 60_000,
    retry: false,
  })
}

export function useSqlWhoAmI(enabled = true) {
  return useQuery({
    queryKey: ['sql', 'whoami'],
    queryFn: () => sqlApi.whoami(),
    staleTime: 5 * 60_000,
    enabled,
    retry: false,
  })
}

/**
 * 发问用的身份。/query 和 A2A 的请求里都要带 user_id，而绑定了用户的 Key 只接受它自己的那个——
 * 拿占位值去问，得到的是一个莫名其妙的 403。目录里有就用目录的，没有再问 /whoami。
 */
export function useSqlIdentity() {
  const catalog = useSqlCatalog()
  const whoami = useSqlWhoAmI(catalog.isError)
  const identity = catalog.data?.identity ?? whoami.data?.identity ?? null
  const service = catalog.data?.service ?? whoami.data?.service ?? null
  return { identity, service, loading: catalog.isLoading || (catalog.isError && whoami.isLoading) }
}

/**
 * 历史的查询（key 和请求）。历史页、问数页的"最近问过"和问完之后回填耗时都用它，共用同一份缓存。
 * key 里带着身份：换了身份就换一份，上一个身份问过的问题不会留在页面上。
 */
export const sqlHistoryQuery = (userId: string) => ({
  queryKey: ['sql', 'history', userId] as const,
  queryFn: () => sqlApi.history({ limit: 100, userId }),
})

export function useSqlHistory(userId = '', enabled = true) {
  return useQuery({ ...sqlHistoryQuery(userId), enabled, retry: false })
}

/** 一次 run。还在跑的每两秒看一次，结束了就不再轮询 */
export function useSqlRun(runId: string) {
  return useQuery({
    queryKey: ['sql', 'run', runId],
    queryFn: () => sqlApi.run(runId),
    enabled: !!runId,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2_000 : false),
    retry: false,
  })
}

/**
 * run 的事件日志。和 run 一起轮询：run 结束之后再取一次就是全的。
 * persisted：服务端关了运行记录时只有内存里的状态，没有事件可取，先看 run 再决定取不取。
 */
export function useSqlRunEvents(runId: string, running: boolean, persisted: boolean) {
  return useQuery({
    queryKey: ['sql', 'run-events', runId],
    queryFn: async () => {
      // 一页最多 1000 条；正常一次 run 几十条，超出时接着往后翻（next_after），最多翻 5 页
      const first = await sqlApi.runEvents(runId)
      const events = [...first.events]
      let page = first
      for (let round = 0; round < 4 && page.events.length >= 1000; round += 1) {
        page = await sqlApi.runEvents(runId, page.next_after)
        events.push(...page.events)
      }
      return { ...page, events }
    },
    enabled: !!runId && persisted,
    refetchInterval: running ? 2_000 : false,
    retry: false,
  })
}
