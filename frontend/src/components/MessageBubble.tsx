import { useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Chip } from './ui'
import { Icon } from './Icon'
import { TurnExplanation } from './TurnExplanation'
import { TurnNotes, TurnPending } from './RoutingInspector'
import { useRestoredTurn } from '@/hooks/useRestoredTurn'
import { agentIdentity, agentLabel } from '@/lib/format'
import { href } from '@/lib/route'
import { unauthorizedKey } from '@/lib/errors'
import { llmReasonKey, llmRecovery } from '@/lib/llm'
import { useT } from '@/i18n'
import { useTicket } from '@/api/hooks'
import type { Key } from '@/i18n/catalog'
import { fallbackAgent, type ChatMessage } from '@/hooks/useChatStream'
import type {
  BusinessDataTrace, KnowledgeSource, RoutingConfig, StreamProgressEvent, TicketNotification,
} from '@/api/types'

/**
 * 单条消息。
 *
 * 这是客服工作台，不是调试面板：默认只呈现「谁答的 + 回复」，再加上员工
 * 把这条回复转述给客户之前**必须知道**的三件事——
 *   · 依据了哪些知识（能点开原文核对）；
 *   · 订单数据是不是真的查到了、算不算数；
 *   · 需要人工的话，工单建没建、有没有人被通知。
 * 还有一种不是回复的东西：模型服务不可用、这一轮没有作答。它和转人工分开说
 * （见 UnavailableNote），不能长得像一条回答，也不能长得像一张工单。
 * 通道、打分、阈值、耗时属于学习模式和编排检查器，默认不出现。
 *
 * 分阶段渲染：SSE 序列是 meta → routing → delta* → result，
 * routing 之前不知道谁在答，result 之前不知道处置结果，都不提前显示。
 */

/** 本轮放进上下文的知识片段。每条能点开所在的文档。 */
function Sources({ sources }: { sources: KnowledgeSource[] }) {
  const { t } = useT()
  return (
    <details className="sources">
      <summary>
        <Icon name="book" size={14} />
        {t('cw.sources')}
        <span className="sources-count">{t('cw.sources.n', { n: sources.length })}</span>
      </summary>
      <p className="sources-note">{t('cw.sources.note')}</p>
      <ol>
        {sources.map((s) => (
          <li key={s.chunk_id || `${s.title}-${s.passage}`}>
            <p className="sources-head">
              {s.doc_id
                ? <a href={href.document(s.doc_id)}>{s.doc_title || s.title || t('kb.untitled')}</a>
                : <span>{s.doc_title || s.title || t('kb.untitled')}</span>}
              <span className="sources-meta">
                {t('cw.source.passage', { n: s.passage + 1 })}
                {!s.doc_id && <> · {t('cw.source.unlinked')}</>}
              </span>
            </p>
            <p className="sources-excerpt">{s.excerpt}</p>
          </li>
        ))}
      </ol>
    </details>
  )
}

/**
 * 订单数据这一轮的结局里，员工转述前需要知道的那几种。
 * 没接、没查、下游判断不用查，都不是需要提醒的事，学习模式里才讲。
 */
/**
 * 下游（SQL Agent）透传上来的阶段。契约 §4.5 只把 stage 值当契约面，文案归调用方；
 * 后端给的 label 只有中文，以前照搬它，英文界面里这几步就是中文。
 * 不在表里的阶段（契约以后新增的）仍用后端给的 label，不吞掉。
 */
const DOWNSTREAM_STAGES: Record<string, { plain: Key; withDb?: Key }> = {
  routing: { plain: 'cw.progress.sql.routing' },
  retrieval: { plain: 'cw.progress.sql.retrieval' },
  planning: { plain: 'cw.progress.sql.planning' },
  generation: { plain: 'cw.progress.sql.generation', withDb: 'cw.progress.sql.generation.db' },
  execution: { plain: 'cw.progress.sql.execution', withDb: 'cw.progress.sql.execution.db' },
  repair: { plain: 'cw.progress.sql.repair' },
  answering: { plain: 'cw.progress.sql.answering' },
}

/** 库标识符的叫法。契约只钉了形状（`*_db`），新增的库照常出现，认不出就显示标识符本身。 */
const DATABASE_NAMES: Record<string, Key> = {
  erp_db: 'cw.db.erp_db',
  wms_db: 'cw.db.wms_db',
  prod_db: 'cw.db.prod_db',
  svc_db: 'cw.db.svc_db',
}

/** 工具循环里专家作答途中自己发起的查询（后端 helpdesk.tool_call）。 */
const TOOL_KEYS: Record<string, Key> = {
  knowledge_search: 'cw.tool.knowledge_search',
  order_data_query: 'cw.tool.order_data_query',
}

/** 发了 tool_call、还没等到对应 tool_result 的那些调用。 */
export function pendingTools(progress: StreamProgressEvent[]): StreamProgressEvent[] {
  const finished = new Set(
    progress.filter((p) => p.stage === 'helpdesk.tool_result').map((p) => p.call_id),
  )
  return progress.filter((p) => p.stage === 'helpdesk.tool_call' && !finished.has(p.call_id))
}

/**
 * 作答途中的状态行。工具循环打开时，专家答到一半可能停下来查东西——查业务系统一次
 * 十几秒到四十多秒，这段时间回复不动，不说一声的话看起来像卡住了。只说正在查什么，
 * 查完就消失；查到了什么、算不算依据，等 result 到了由下面的依据和学习模式去说。
 */
function ToolStatus({ progress }: { progress: StreamProgressEvent[] }) {
  const { t } = useT()
  const pending = pendingTools(progress)
  const current = pending[pending.length - 1]
  if (!current) return null
  return (
    <div className="thinking" aria-live="polite">
      <span className="pulse" />
      {t(TOOL_KEYS[current.tool ?? ''] ?? 'cw.tool.other', { query: current.query ?? '' })}
    </div>
  )
}

/** 准备阶段每一步的说法：本系统自己的阶段和下游认得出的阶段都按界面语言出文案。 */
function progressLabel(
  p: StreamProgressEvent, t: (key: Key, vars?: Record<string, string | number>) => string,
): string {
  if (p.stage === 'helpdesk.understanding') return t('cw.progress.understanding')
  if (p.stage === 'helpdesk.gathering') {
    if (p.knowledge && p.order_data) return t('cw.progress.both')
    if (p.order_data) return t('cw.progress.data')
    if (p.knowledge) return t('cw.progress.knowledge')
  }
  const known = DOWNSTREAM_STAGES[p.stage]
  if (!known) return p.label
  if (known.withDb && p.database) {
    const name = DATABASE_NAMES[p.database]
    return t(known.withDb, { db: name ? t(name) : p.database })
  }
  return t(known.plain)
}

/** 对话请求失败时，按状态码给的下一步；0 是根本没连上服务端 */
const ERROR_HINT: Record<number, Key> = { 503: 'err.503', 0: 'err.network' }

function DataNote({ data }: { data: BusinessDataTrace }) {
  const { t } = useT()
  switch (data.outcome) {
    case 'evidence':
      return (
        <p className="turn-note">
          <Icon name="check" size={14} />
          {t('cw.data.evidence', { dbs: (data.databases ?? []).join(', ') || t('cw.data.systems') })}
        </p>
      )
    case 'unverified':
      return <p className="note caution">{t('cw.data.unverified')}</p>
    case 'failed':
    case 'unavailable':
    case 'error':
      return <p className="note caution">{t('cw.data.failed')}</p>
    case 'empty':
      return <p className="note neutral">{t('cw.data.empty')}</p>
    default:
      return null
  }
}

const NOTIFY_KEYS: Record<string, Key> = {
  pending: 'cw.ticket.notify.queued',
  queued: 'cw.ticket.notify.queued',
  delivered: 'cw.ticket.notify.delivered',
  failed: 'cw.ticket.notify.failed',
  not_configured: 'cw.ticket.notify.none',
}

const STATUS_KEYS: Record<string, Key> = {
  open: 'tk.status.open',
  assigned: 'tk.status.assigned',
  resolved: 'tk.status.resolved',
  cancelled: 'tk.status.cancelled',
}

/**
 * 转人工的处置结果。**只说后端说过的话**：
 * 工单建了就给工单号和它现在的状态；通知按投递记录说（没配 / 排队 / 送达 / 失败）。
 * "已有人接手""会回电"这类话后端没有依据，这里一句都不出现。
 */
export function TicketNote({ ticketId, status, notification }: {
  ticketId: string
  /** 工单当前状态；拿不到就不显示 */
  status?: string
  /** 通知状态：投递记录的 status，或 not_configured；undefined = 后端没报 */
  notification?: string
}) {
  const { t } = useT()
  const statusKey = status ? STATUS_KEYS[status] : undefined
  const notifyKey = notification ? NOTIFY_KEYS[notification] : undefined
  // 工单已经被人流转过（已分配 / 已解决 / 已取消）之后，"需要人工跟进""还在等处理"
  // 就成了过时的话——这张卡片会一直留在对话里，得跟着工单现在的状态说
  const moved = !!status && status !== 'open'
  return (
    <div className="ticket-note">
      <p className="ticket-note-head">
        <Icon name="person" size={14} />
        <strong>{t(moved ? 'cw.handoff.moved' : 'cw.handoff')}</strong>
      </p>
      <p className="ticket-note-line">
        {t('cw.ticket.created')} <span className="mono">{ticketId}</span>
        {statusKey && <Chip>{t(statusKey)}</Chip>}
        <a className="ticket-note-link" href={href.ticket(ticketId)}>{t('cw.ticket.view')}</a>
      </p>
      {notifyKey && !moved && <p className="ticket-note-sub">{t(notifyKey)}</p>}
    </div>
  )
}

const NEXT_KEYS: Record<ReturnType<typeof llmRecovery>, Key> = {
  reset: 'cw.unavailable.next.reset',
  wait: 'cw.unavailable.next.wait',
  retry: 'cw.unavailable.next.retry',
}

/**
 * 模型服务不可用、这一轮没有作答。
 *
 * 和转人工是两件事，所以这里**没有**"需要人工跟进"的卡片：那张卡片的意思是
 * 已经判断过这件事需要人来处理、而且建了工单；这里是没能判断，也没有工单。
 * 停机时把每一条没答上来的提问都显示成转人工，员工会以为它们都有人在跟。
 * 客户确实要人工时怎么办也写在这里——那句口令走规则，模型不可用时照样建得了单。
 */
export function UnavailableNote({ reason }: { reason?: string | null }) {
  const { t } = useT()
  return (
    <div className="note danger unavailable-note" role="alert">
      <p><strong>{t('cw.unavailable.title')}</strong> {t('cw.unavailable.body')}</p>
      <p className="unavailable-cause">
        {t('cw.unavailable.cause', { reason: t(llmReasonKey(reason)) })}{' '}
        {t(NEXT_KEYS[llmRecovery(reason)])}{' '}
        <a href={href.admin()}>{t('cw.unavailable.status')}</a>
      </p>
    </div>
  )
}

/** 带实时状态的工单卡：别人把工单标成「已分配」后，这里跟着变。取不到就用当轮响应里的。 */
function LiveTicketNote({ base, ticketId, initialStatus, initialNotification }: {
  base: string; ticketId: string; initialStatus?: string; initialNotification?: string
}) {
  const { data: live, isError } = useTicket(base, ticketId)
  const delivery: TicketNotification | null | undefined = live?.notification
  const notification = delivery ? delivery.status
    : delivery === null && live?.notifications_enabled === false ? 'not_configured'
      : initialNotification || undefined
  // 实时状态取不到（比如没有员工钥匙）时不猜：工单可能已经被人流转过了
  const status = live?.status ?? (isError ? undefined : initialStatus)
  return <TicketNote ticketId={ticketId} status={status} notification={notification} />
}

/**
 * 学习模式里刷新前的那一轮：点一下才去事件日志取（历史有几十条时，不为每一条都发请求），
 * 取回来照当轮的样子讲。取不到时说清楚为什么。
 */
function RestoredExplanation({ message, base, routingConfig, onInspect }: {
  message: ChatMessage
  base: string
  routingConfig?: RoutingConfig
  onInspect?: () => void
}) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const turn = useRestoredTurn(base, message, open)
  if (!open) {
    return (
      <button type="button" className="btn btn-secondary restored-turn" onClick={() => setOpen(true)}>
        <Icon name="panel" size={14} />{t('cw.turn.show')}
      </button>
    )
  }
  if (turn.state !== 'ready') return <TurnPending turn={turn} />
  return (
    <>
      <TurnNotes redacted={turn.redacted} complete={turn.complete} />
      <TurnExplanation message={turn.message} routingConfig={routingConfig} onInspect={onInspect} />
    </>
  )
}

export function MessageBubble({ message, onInspect, explain, routingConfig, base }: {
  message: ChatMessage
  onInspect?: () => void
  explain?: boolean
  /** /health 里的编排阈值，学习模式用它讲门槛；拿不到就不讲具体数字 */
  routingConfig?: RoutingConfig
  /** 传了才去取工单的实时状态 */
  base?: string
}) {
  const { t } = useT()
  const { routing, result, meta } = message

  if (message.role === 'user') {
    return (
      <article className="turn user">
        <div className="bubble">{message.content}</div>
      </article>
    )
  }

  if (message.error) {
    // 认得出原因的先说该怎么办，后端的原话跟在后面；认不出的照旧只给原话
    const hint = message.errorStatus === 401 ? unauthorizedKey() : ERROR_HINT[message.errorStatus ?? -1]
    return (
      <article className="turn assistant">
        <p className="note danger" role="alert">
          <strong>{t('cw.error')}</strong>{' '}
          {hint ? (
            <>
              {t(hint)}
              {message.errorStatus === 401 && <> <a href={href.admin()}>{t('cw.error.openConnection')}</a></>}
              <span className="mono state-detail"> {message.error}</span>
            </>
          ) : <span>{message.error}</span>}
        </p>
      </article>
    )
  }

  // 刷新后从审计日志恢复的历史：只有原文和作答 Agent，没有路由/置信度，
  // 不能走下面「routing 还没到 = 正在编排」那条分支，否则永远转圈
  if (message.restored) {
    const agent = message.agentType ? agentIdentity(message.agentType, t)
        : { name: t('agent.reply'), mark: '·' }
    return (
      <article className="turn assistant restored">
        <div className="chip-row">
          <p className="turn-role">{agent.name}</p>
          <Chip>{t('cw.history')}</Chip>
          {/* 审计日志里只留了"这句是系统提示"，原因没留——标出没作答，不编原因 */}
          {message.agentType === 'system' && <Chip tone="danger">{t('cw.unavailable')}</Chip>}
        </div>
        <div className="body">
          <div className="prose-chat">
            <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
          </div>
        </div>
        {message.ticketId && (base
          ? <LiveTicketNote base={base} ticketId={message.ticketId} />
          : <TicketNote ticketId={message.ticketId} />)}
        {explain && message.turnId && base && (
          <RestoredExplanation message={message} base={base} routingConfig={routingConfig} onInspect={onInspect} />
        )}
      </article>
    )
  }

  // 模型服务不可用、没有作答。放在下面那个分支之前：这一轮可能根本没有 routing
  // （老的流式事件序列），不能因此一直显示"正在处理"。
  if (result?.service_unavailable) {
    return (
      <article className="turn assistant unavailable">
        <div className="chip-row">
          <p className="turn-role">{agentIdentity('system', t).name}</p>
          <Chip tone="danger">{t('cw.unavailable')}</Chip>
        </div>
        {/* 后端给客户的那句原话照样显示：员工要知道客户端看到的是什么 */}
        <div className="body">
          <div className="prose-chat">
            <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
          </div>
        </div>
        <UnavailableNote reason={result.unavailable_reason} />
        {explain && (
          <TurnExplanation message={message} routingConfig={routingConfig} onInspect={onInspect} />
        )}
      </article>
    )
  }

  // routing 还没到 —— 谁在答都不知道，不编造身份。
  // 但准备阶段（记忆、意图、检索、委派查库）本身就要十几到四十秒，光转圈等于
  // 让客户盯着一个不动的点看半分钟。有 progress 就把它铺出来：最后一条是当前
  // 在做的事，前面的划掉表示已完成。后端不发 progress 时（非流式、老后端）这里
  // 退回原来的转圈，行为不变。
  if (!routing?.primary_agent) {
    const steps = message.progress ?? []
    return (
      <article className="turn assistant" aria-live="polite">
        {steps.length === 0 ? (
          <div className="thinking"><span className="pulse" />{t('cw.orchestrating')}</div>
        ) : (
          <ol className="progress-steps">
            {steps.map((p, i) => {
              // 最后一条是正在做的。"查找资料"是个例外：它后面跟着的是下游查库的细分
              // 阶段，那些阶段在推进不代表知识检索也完了——整段准备结束之前它都算在做
              const active = i === steps.length - 1 || p.stage === 'helpdesk.gathering'
              return (
                <li key={`${p.stage}-${i}`} className={active ? 'active' : 'done'}>
                  {active ? <span className="pulse" /> : <Icon name="check" size={12} />}
                  {progressLabel(p, t)}
                </li>
              )
            })}
          </ol>
        )}
      </article>
    )
  }

  const support = routing.supporting_agents ?? []
  // 选中的专家没答完、兜底 Agent 接手的那种轮次（只有非流式会这样：流式失败不重跑）。
  // 后端如实报了两件事——选的是 primary_agent，作答的是 agent_type——这里照着说，
  // 不把兜底写的回复挂在专家头上。刷新后审计日志里记的也是实际作答的那一个，
  // 两处的署名得对得上。
  const fallback = fallbackAgent(routing, result, routingConfig)
  const agent = agentIdentity(fallback ?? routing.primary_agent, t)
  const cut = result?.policy_violations?.[0]
  const sources = meta?.knowledge_sources ?? []
  const data = meta?.business_data

  return (
    <article className="turn assistant">
      <div className="chip-row">
        <p className="turn-role">
          {agent.name}
          {support.length > 0 && (
            <>
              <span className="sep">·</span>
              <span className="support">＋ {support.map((x) => agentLabel(x, t)).join(', ')}</span>
            </>
          )}
        </p>
        {result?.needs_clarification && <Chip tone="caution">{t('cw.needInfo')}</Chip>}
      </div>

      <div className="body">
        {/* 流式下每次增量都重渲染半截 Markdown——未闭合的语法先当普通文本，
            补全后自然变成格式。react-markdown 默认不解析原始 HTML。 */}
        <div className="prose-chat">
          <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
        </div>
        {message.streaming && <span className="caret" />}
      </div>
      {message.streaming && <ToolStatus progress={message.progress ?? []} />}

      {fallback && (
        <p className="note caution">
          {t('cw.fallback', { agent: agentIdentity(routing.primary_agent, t).name })}
        </p>
      )}

      {result?.handoff && (
        result.ticket_id
          ? base
            ? <LiveTicketNote base={base} ticketId={result.ticket_id}
                              initialStatus={result.ticket_status || undefined}
                              initialNotification={result.ticket_notification || undefined} />
            : <TicketNote ticketId={result.ticket_id} status={result.ticket_status || undefined}
                          notification={result.ticket_notification || undefined} />
          // 判了转人工但没有工单：要说出来。显示成"已转人工"的话，这件事就没人跟了。
          : <p className="note danger" role="alert">{t('cw.ticket.notCreated')}</p>
      )}

      {cut && (
        <div className="redaction">
          <p className="redaction-head">
            <Icon name="shield" size={14} />
            {t('cw.cut', { n: result!.policy_violations!.length })} · {t(`policy.${cut.rule}` as Key)}
          </p>
          <p className="redaction-cut">{cut.sentence}</p>
        </div>
      )}

      {result && data && <DataNote data={data} />}
      {result && sources.length > 0 && <Sources sources={sources} />}

      {explain && result && (
        <TurnExplanation message={message} routingConfig={routingConfig} onInspect={onInspect} />
      )}

      <div className="turn-foot">
        <span className="turn-actions">
          <button
            className="icon-btn" title={t('cw.copy')} aria-label={t('cw.copy')}
            onClick={() => navigator.clipboard?.writeText(message.content)}
          >
            <Icon name="copy" size={16} />
          </button>
        </span>
      </div>
    </article>
  )
}
