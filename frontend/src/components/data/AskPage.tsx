/**
 * 问数（#/data/ask）：问一句，看结论、数字和表格，接着追问（移植自 SQL Agent 旧控制台的 views/ask.js）。
 *
 * 没问过的时候是首页（输入框、"可以这样问"、"最近问过"），问了之后是对话：问题在右、答案在左，
 * 输入框停在底部。一轮问答是一个 Turn；界面状态（折叠、翻页、看图还是看表、反馈草稿）在各自组件里，
 * key 不变 React 就留着——切语言、切学习模式、回填耗时都不会把用户展开的东西收回去。
 *
 * 问题走 A2A 的 message/stream（有真实的阶段进度，首帧就拿到 run_id，停止时能请服务端也停下）；
 * 服务端说流式没开（-32004）就退回 POST /query。请求都经 /sql-api/，浏览器里没有 SQL Agent 的 Key。
 *
 * 这一页切走时只隐藏不卸载（DataPanel）：问题可能要跑几十秒，去别的页看一眼再回来不能丢。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SqlApiError, isAbort, sqlApi, streamQuery } from '@/api/sql/client'
import { sqlHistoryQuery, useSqlCatalog, useSqlHistory, useSqlIdentity } from '@/api/sql/hooks'
import type { Identity, ServiceInfo } from '@/api/sql/types'
import { useT } from '@/i18n'
import { announcement as announce } from '@/lib/sql/ask-summary'
import { onAskDraft, takeAskDraft, type AskDraft } from '@/lib/sql/askDraft'
import { explainSqlError } from '@/lib/sql/errors'
import { normalizeResult } from '@/lib/sql/model'
import { AskHome, recentItems } from './ask/AskHome'
import { TurnView } from './ask/Answer'
import { Composer } from './ask/Composer'
import { newSessionId, newTurnId, type Transport, type Turn } from './ask/turn'
import './ask/ask.css'

// 发送键在发出问题后原地变成"停止"。连点两下的第二下不该把刚发出去的问题停掉
const STOP_GUARD_MS = 600
const LEARNING_KEY = 'helpdesk.sql.learning'

function readLearning(): boolean {
  try { return globalThis.localStorage?.getItem(LEARNING_KEY) === 'on' } catch { return false }
}

/** 最近问过：这个身份自己的历史。身份没确定之前不取 */
const useRecentHistory = (userId: string | undefined) => useSqlHistory(userId ?? '', !!userId)

export function AskPage({ active }: { active: boolean }) {
  const { t, lang } = useT()
  const queryClient = useQueryClient()
  const catalogQuery = useSqlCatalog()
  const catalog = catalogQuery.data
  const { identity, service } = useSqlIdentity()
  const recent = useRecentHistory(identity?.user_id)

  const [turns, setTurns] = useState<Turn[]>([])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [draftNote, setDraftNote] = useState('')
  const [learning, setLearning] = useState(readLearning)
  const [now, setNow] = useState(() => performance.now())
  const [spoken, setSpoken] = useState('')

  const session = useRef(newSessionId())
  const controllers = useRef(new Map<string, AbortController>())
  const runIds = useRef(new Map<string, string>())
  const busy = useRef<{ id: string; startedAt: number } | null>(null)
  const streamUnsupported = useRef(false)
  // 从查询历史"再问一次"交过来的问题：发出时开一段新对话（见 send）
  const rerunPending = useRef(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const latest = useRef<{ identity: Identity | null; service: ServiceInfo | null }>({ identity, service })
  latest.current = { identity, service }
  const textContext = useRef({ t, lang, catalog })
  textContext.current = { t, lang, catalog }

  const update = useCallback((id: string, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) => {
    setTurns((all) => all.map((turn) => (turn.id === id
      ? { ...turn, ...(typeof patch === 'function' ? patch(turn) : patch) }
      : turn)))
  }, [])

  /**
   * 发问之前先确认身份与服务能力（通常已经有了；页面刚打开时要等几十毫秒）。没有它，user_id 只能填
   * 占位值，绑定了用户的 Key 会因此被拒；也不知道该不该走流式。目录拿不到不影响这一步——身份另有来源。
   */
  const resolveIdentity = useCallback(async () => {
    if (latest.current.identity) return latest.current
    try {
      const fresh = await queryClient.fetchQuery({ queryKey: ['sql', 'catalog'], queryFn: () => sqlApi.catalog() })
      return { identity: fresh.identity, service: fresh.service }
    } catch {
      try {
        const who = await queryClient.fetchQuery({ queryKey: ['sql', 'whoami'], queryFn: () => sqlApi.whoami() })
        return { identity: who.identity, service: who.service }
      } catch {
        // 连身份也问不到：照常发问，服务不在、Key 不对，都由那次请求如实报出来
        return { identity: null, service: null }
      }
    }
  }, [queryClient])

  /** 服务端记账的耗时、token 与检索命中的表不在问答的响应里，查完从 /history 取回 */
  const backfill = useCallback(async (turnId: string, userId: string, traceId: string) => {
    if (!traceId) return
    try {
      const history = await queryClient.fetchQuery({ ...sqlHistoryQuery(userId), staleTime: 0 })
      const entry = (history.items ?? []).find((item) => item.trace_id === traceId)
      if (!entry) return
      update(turnId, {
        backfill: {
          serverMs: typeof entry.duration_ms === 'number' ? entry.duration_ms : null,
          totalTokens: typeof entry.total_tokens === 'number' ? entry.total_tokens : null,
          candidateTables: Array.isArray(entry.tables) ? entry.tables : [],
        },
      })
    } catch { /* 回填不到只是少几项细节，不影响结果 */ }
  }, [queryClient, update])

  const ask = useCallback(async (question: string, { rerun = false }: { rerun?: boolean } = {}) => {
    const text = question.trim()
    if (!text || busy.current) return
    const turn: Turn = {
      id: newTurnId(), question: text, askedAt: new Date().toISOString(), status: 'pending', result: null,
      error: null, transport: 'query', stages: [], stageNow: null, startedAt: performance.now(), runId: '',
      cancel: '', rerun, backfill: null,
    }
    const controller = new AbortController()
    controllers.current.set(turn.id, controller)
    busy.current = { id: turn.id, startedAt: turn.startedAt }
    setBusyId(turn.id)
    setNow(performance.now())
    setTurns((all) => [...all, turn])

    let status: Turn['status'] = 'error'
    let result: Turn['result'] = null
    let userId = ''
    try {
      const resolved = await resolveIdentity()
      if (controller.signal.aborted) throw new DOMException('stopped before sending', 'AbortError')
      userId = resolved.identity?.user_id || 'anonymous'
      let transport: Transport = resolved.service?.streaming && !streamUnsupported.current ? 'stream' : 'query'
      update(turn.id, { transport })

      let payload: Record<string, unknown>
      let stages: Turn['stages'] = []
      const query = async () => {
        const answered = await sqlApi.query({ query: text, user_id: userId, session_id: session.current }, controller.signal)
        if (answered.runId) {
          runIds.current.set(turn.id, answered.runId)
          update(turn.id, { runId: answered.runId })
        }
        return answered.response as unknown as Record<string, unknown>
      }
      if (transport === 'stream') {
        try {
          const streamed = await streamQuery(text, {
            userId,
            sessionId: session.current,
            signal: controller.signal,
            onStage: (stage) => update(turn.id, (was) => ({ stages: [...was.stages, stage], stageNow: stage })),
            onTask: (runId) => {
              runIds.current.set(turn.id, runId)
              update(turn.id, { runId })
            },
          })
          payload = streamed.payload
          stages = streamed.stages
        } catch (error) {
          // 服务端明确说流式没开（-32004）：这是"换条路"，不是失败。其余错误不重试——流已经打开过的话，
          // 重发等于让同一个问题再跑一遍
          if (!(error instanceof SqlApiError) || error.code !== 'stream_unsupported') throw error
          streamUnsupported.current = true
          transport = 'query'
          update(turn.id, { transport, stages: [], stageNow: null })
          payload = await query()
        }
      } else {
        payload = await query()
      }

      result = normalizeResult(payload, { transport, stages, clientMs: performance.now() - turn.startedAt })
      status = 'done'
      update(turn.id, { status, result })
    } catch (error) {
      status = isAbort(error) ? 'stopped' : 'error'
      update(turn.id, status === 'stopped' ? { status } : { status, error })
    } finally {
      controllers.current.delete(turn.id)
      busy.current = null
      setBusyId(null)
      setSpoken(announce(status, text, result, textContext.current))
    }
    if (status === 'done' && result) void backfill(turn.id, userId, result.traceId)
    else if (status === 'stopped' && userId) void queryClient.invalidateQueries({ queryKey: sqlHistoryQuery(userId).queryKey })
  }, [backfill, queryClient, resolveIdentity, update])

  /**
   * 停止：先请服务端停下这次运行，再断开连接。服务端默认在连接断开后也会停下
   * （AI_SQL_CANCEL_ON_DISCONNECT），显式取消不依赖这个开关，还能知道服务端那边的结果：受理了、
   * 已经跑完（409），还是请求失败（失败不影响本地的停止）。停止提示按这个结果写。
   * 运行号只有流式请求在首帧里拿得到（/query 要等跑完才在响应头里给）。
   */
  const stopTurn = useCallback((turnId: string) => {
    const controller = controllers.current.get(turnId)
    if (!controller) return
    const runId = runIds.current.get(turnId)
    if (runId) {
      update(turnId, { cancel: 'sent' })
      sqlApi.cancelRun(runId)
        .then(() => update(turnId, { cancel: 'accepted' }))
        .catch((error) => update(turnId, {
          cancel: error instanceof SqlApiError && error.status === 409 ? 'finished' : 'failed',
        }))
    }
    controller.abort()
  }, [update])

  /** 输入框上的停止键：发出后不到 0.6 秒的那一下多半是连点，不算 */
  const stopCurrent = useCallback(() => {
    const current = busy.current
    if (current && performance.now() - current.startedAt > STOP_GUARD_MS) stopTurn(current.id)
  }, [stopTurn])

  /** 新对话：清掉页面上的这段对话，换一个会话（服务端按会话记上下文） */
  const newConversation = useCallback(() => {
    if (busy.current) return
    setTurns([])
    session.current = newSessionId()
    rerunPending.current = false
    setDraftNote('')
    inputRef.current?.focus()
  }, [])

  /**
   * 发出输入框里的问题。从查询历史"再问一次"交过来的问题必须换一个新会话：沿用当前会话的话，服务端的
   * 路由会发现"这个问题刚才问过、结果还在上下文里"，直接根据记忆作答而不去查库——那样拿回来的就是旧结果
   * 的复述，而页面却在说这是刚读出来的数据。
   */
  const send = useCallback((text: string) => {
    if (!text.trim() || busy.current) return
    const rerun = rerunPending.current
    if (rerun) {
      setTurns([])
      session.current = newSessionId()
      rerunPending.current = false
      setDraftNote('')
    }
    setInput('')
    void ask(text, { rerun })
  }, [ask])

  /** 把一句话放进输入框，光标停在末尾。只填不发——问什么、怎么改，由人定 */
  const prefill = useCallback((text: string) => {
    setInput(text)
    window.setTimeout(() => {
      const element = inputRef.current
      if (!element) return
      element.focus()
      element.setSelectionRange(text.length, text.length)
    }, 0)
  }, [])

  // 别的页面交过来的问题（查询历史、一次查询的经过、数据浏览）：打开时取走，开着（只是藏起来）时也接
  const applyDraft = useCallback((draft: AskDraft) => {
    rerunPending.current = Boolean(draft.note)
    setDraftNote(draft.note ?? '')
    if (draft.send && !busy.current) send(draft.question)
    else prefill(draft.question)
  }, [prefill, send])

  useEffect(() => {
    const take = () => {
      const draft = takeAskDraft()
      if (draft) applyDraft(draft)
    }
    take()
    return onAskDraft(take)
  }, [applyDraft])

  // 切回这一页时，上一次没取成的"最近问过"再取一次（只在切回来的那一下，不是每次重画）
  const recentRef = useRef(recent)
  recentRef.current = recent
  useEffect(() => {
    if (active && recentRef.current.isError) void recentRef.current.refetch()
  }, [active])

  // 等结果时每秒走一次表（"12 秒"）
  useEffect(() => {
    if (!busyId) return
    const timer = window.setInterval(() => setNow(performance.now()), 1000)
    return () => window.clearInterval(timer)
  }, [busyId])

  // 离开页面（整个控制台卸载）时，还在跑的请求一并断开
  useEffect(() => {
    const open = controllers.current
    return () => { for (const controller of open.values()) controller.abort() }
  }, [])

  const toggleLearning = (on: boolean) => {
    setLearning(on)
    try { globalThis.localStorage?.setItem(LEARNING_KEY, on ? 'on' : 'off') } catch { /* 只在这一页有效 */ }
  }

  const lastQuestion = turns.length ? turns[turns.length - 1]!.question : ''
  const offlineError = catalogQuery.error instanceof SqlApiError
    && [0, 401, 502, 503, 504].includes(catalogQuery.error.status) ? catalogQuery.error : null
  const offline = offlineError && (() => {
    const { body, detail } = explainSqlError(offlineError, t)
    return (
      <div className="note danger sq-ask-offline" role="note">
        <p className="sq-notice-title">{t('sq.ask.offline')}</p>
        <p>{body}</p>
        {detail && <p className="sq-server-text mono">{detail}</p>}
      </div>
    )
  })()

  const composer = (
    <>
      {draftNote && (
        <div className="note neutral sq-draft-note" role="note">
          <span>{draftNote}</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => {
            setDraftNote('')
            rerunPending.current = false
          }}>{t('sq.ask.draftDismiss')}</button>
        </div>
      )}
      <Composer value={input} onChange={(value) => {
        setInput(value)
        if (!value.trim() && draftNote) {
          setDraftNote('')
          rerunPending.current = false
        }
      }} onSubmit={() => send(input)} onStop={stopCurrent} busy={!!busyId} docked={turns.length > 0}
        lastQuestion={lastQuestion} inputRef={inputRef} onNew={newConversation} />
    </>
  )

  return (
    <div className="sq-ask" data-active={active}>
      <div className="sq-ask-toolbar">
        <label className="switch learn-switch" title={t('sq.ask.learn.hint')}>
          <input type="checkbox" checked={learning} onChange={(event) => toggleLearning(event.target.checked)} />
          <span className="track" /><span className="switch-text">{t('sq.ask.learn')}</span>
        </label>
      </div>
      <div className="sq-ask-scroll scroll">
        {turns.length === 0
          ? (
            <AskHome catalog={catalog} catalogLoading={catalogQuery.isLoading} recent={recentItems(recent.data?.items)}
                     composer={composer} offline={offline} onPick={prefill} />
          )
          : (
            <div className="sq-turns">
              <h2 className="sr-only">{t('sq.ask.conversation')}</h2>
              {turns.map((turn) => (
                <TurnView key={turn.id} turn={turn} catalog={catalog} service={service} learning={learning} now={now}
                          onStop={() => stopTurn(turn.id)} onAskAgain={() => { void ask(turn.question) }}
                          onFollowUp={() => inputRef.current?.focus()} />
              ))}
            </div>
          )}
      </div>
      {turns.length > 0 && <div className="sq-ask-dock">{composer}</div>}
      <div className="sr-only sq-ask-announce" role="status" aria-live="polite">{spoken}</div>
    </div>
  )
}
