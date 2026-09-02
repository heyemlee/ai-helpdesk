import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'

/**
 * 只读数据统一走 TanStack Query：自动缓存、去重、轮询和错误状态，
 * 不用在每个组件里手写 loading / error / try-catch。
 */

export function useHealth(base: string) {
  return useQuery({
    queryKey: ['health', base],
    queryFn: () => api.health(base),
    refetchInterval: 15_000,
    retry: false,
  })
}

export function useMonitor(base: string, enabled = true) {
  return useQuery({
    queryKey: ['monitor', base],
    queryFn: () => api.monitor(base),
    // Agent 统计是实时变化的，5 秒轮询让面板能看到路由权重的漂移
    refetchInterval: 5_000,
    enabled,
    retry: false,
  })
}

export function useKnowledgeStats(base: string) {
  return useQuery({
    queryKey: ['knowledge-stats', base],
    queryFn: () => api.knowledgeStats(base),
    retry: false,
  })
}

export function useSearch(base: string) {
  return useMutation({
    mutationFn: ({ query, topK }: { query: string; topK?: number }) =>
      api.search(base, query, topK),
  })
}

/** 评测开始时间。放模块级而不是组件里：面板会随切页卸载，计时器要活得比组件久。 */
const evalStartedAt = new Map<string, number>()

/**
 * 跑一次端到端评测。
 *
 * 刻意用 useQuery + 手动 refetch，而不是 useMutation：一次评测是分钟级的
 * （内置用例逐条走真实检索、编排和 LLM 打分），用户很可能中途切去别的页面。
 * useMutation 的状态活在组件里，EvalPanel 一卸载，pending 和结果就都没了——
 * 而请求并不会停（fetch 没有 signal），后端照样跑到底再把结果丢进垃圾桶。
 * 查询缓存挂在 QueryClient 上，切走再切回来还能接上同一次运行。
 */
export function useRunEval(base: string) {
  const { refetch, data, error, isFetching } = useQuery({
    queryKey: ['eval-run', base],
    queryFn: () => api.runEval(base),
    enabled: false,       // 只由「运行评测」按钮触发，不在挂载时自动跑
    retry: false,
    staleTime: Infinity,  // 结果是一次快照，没有"过期后台重取"一说
    // 面板卸载期间这条缓存没有观察者，默认 5 分钟就被回收——比一次评测还短
    gcTime: Infinity,
  })

  const run = useCallback(() => {
    evalStartedAt.set(base, Date.now())
    // cancelRefetch: false —— 已经在跑就复用那一次，不并发第二次评测
    void refetch({ cancelRefetch: false })
  }, [base, refetch])

  return {
    run,
    data,
    // 正在重跑时，别把上一次的失败继续挂在页面上
    error: isFetching ? null : error,
    isPending: isFetching,
    startedAt: evalStartedAt.get(base),
  }
}

export function useAddKnowledge(base: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (docs: Array<{ title: string; content: string }>) => api.addKnowledge(base, docs),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['knowledge-stats', base] }),
  })
}

export function useUploadKnowledge(base: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => api.uploadKnowledge(base, file),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['knowledge-stats', base] }),
  })
}
