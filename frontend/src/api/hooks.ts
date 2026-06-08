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

export function useRunEval(base: string) {
  return useMutation({ mutationFn: () => api.runEval(base) })
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
