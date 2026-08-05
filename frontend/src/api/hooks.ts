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

/** 模型连接的现状。向量切换在跑时每 1.5 秒看一次进度，平时不轮询。 */
export function useModelStatus(base: string) {
  return useQuery({
    queryKey: ['model-status', base],
    queryFn: () => api.modelStatus(base),
    refetchInterval: (query) => (query.state.data?.switch.state === 'running' ? 1_500 : false),
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

/**
 * 知识库的文档列表。query 为空是浏览全部；非空时由后端按字面筛选
 * （每个词都要在标题或正文里出现）。
 *
 * placeholderData 保留上一次的结果：筛选框每敲一个字就换一次 queryKey，
 * 不留着的话列表会在每次按键之间闪成"加载中"。
 */
export function useDocuments(base: string, query: string, category = '') {
  return useQuery({
    queryKey: ['knowledge-documents', base, query, category],
    queryFn: () => api.documents(base, query, category),
    placeholderData: (previous) => previous,
    retry: false,
  })
}

/**
 * 分类和每类的篇数。导入框和阅读页用它列分类；老后端没有这个接口（404），
 * 调用方拿不到数据就不显示分类，其余照常。
 */
export function useCategories(base: string) {
  return useQuery({
    queryKey: ['knowledge-categories', base],
    queryFn: () => api.categories(base),
    staleTime: 30_000,
    retry: false,
  })
}

export function useDocument(base: string, docId: string | undefined) {
  return useQuery({
    queryKey: ['knowledge-document', base, docId],
    queryFn: () => api.document(base, docId!),
    enabled: !!docId,
    retry: false,
  })
}

/** 导入、改分类之后列表、分类篇数和统计都要重新取——"进去了没有、挪过去了没有"得当场看得见。 */
function useInvalidateKnowledge(base: string) {
  const qc = useQueryClient()
  return () => Promise.all([
    qc.invalidateQueries({ queryKey: ['knowledge-stats', base] }),
    qc.invalidateQueries({ queryKey: ['knowledge-documents', base] }),
    qc.invalidateQueries({ queryKey: ['knowledge-categories', base] }),
  ])
}

export function useAddKnowledge(base: string) {
  const invalidate = useInvalidateKnowledge(base)
  return useMutation({
    mutationFn: (docs: Array<{ title: string; content: string; category?: string }>) =>
      api.addKnowledge(base, docs),
    onSuccess: invalidate,
  })
}

export function useUploadKnowledge(base: string) {
  const invalidate = useInvalidateKnowledge(base)
  return useMutation({
    mutationFn: ({ file, category }: { file: File; category?: string }) =>
      api.uploadKnowledge(base, file, category),
    onSuccess: invalidate,
  })
}

export function useSetDocumentCategory(base: string, docId: string) {
  const qc = useQueryClient()
  const invalidate = useInvalidateKnowledge(base)
  return useMutation({
    mutationFn: (category: string) => api.setDocumentCategory(base, docId, category),
    onSuccess: () => Promise.all([
      invalidate(),
      qc.invalidateQueries({ queryKey: ['knowledge-document', base, docId] }),
    ]),
  })
}

export function useSops(base: string) {
  return useQuery({
    queryKey: ['sops', base],
    queryFn: () => api.sops(base),
    retry: false,
  })
}

// ── 工单 ────────────────────────────────────────────────────────────────────

export function useTickets(base: string, status: string) {
  return useQuery({
    queryKey: ['tickets', base, status],
    queryFn: () => api.tickets(base, status),
    placeholderData: (previous) => previous,
    // 工单是别人的对话建出来的，开着页面也要能看到新来的
    refetchInterval: 20_000,
    retry: false,
  })
}

export function useTicket(base: string, ticketId: string | undefined) {
  return useQuery({
    queryKey: ['ticket', base, ticketId],
    queryFn: () => api.ticket(base, ticketId!),
    enabled: !!ticketId,
    retry: false,
  })
}

/**
 * 流转工单状态。**不做乐观更新**：状态机在后端，合不合法由后端说了算；
 * 失败时界面上还是原来的状态，错误交给调用方显示。
 */
export function useSetTicketStatus(base: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ ticketId, status }: { ticketId: string; status: string }) =>
      api.setTicketStatus(base, ticketId, status),
    onSuccess: (ticket) => {
      qc.setQueryData(['ticket', base, ticket.id], ticket)
      return Promise.all([
        qc.invalidateQueries({ queryKey: ['tickets', base] }),
        qc.invalidateQueries({ queryKey: ['health', base] }),
      ])
    },
    // 被拒多半是因为这张工单已经被别人流转过了：重新取一次，让页面显示它现在真正的状态
    onError: (_error, { ticketId }) => qc.invalidateQueries({ queryKey: ['ticket', base, ticketId] }),
  })
}

export function useConversation(base: string, convId: string | undefined) {
  return useQuery({
    queryKey: ['conversation', base, convId],
    queryFn: () => api.conversation(base, convId!),
    enabled: !!convId,
    retry: false,
  })
}
