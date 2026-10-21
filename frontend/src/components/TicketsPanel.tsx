import { useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useConversation, useSetTicketStatus, useTicket, useTickets } from '@/api/hooks'
import { ApiError } from '@/api/client'
import { Chip, StateBlock, type Tone } from './ui'
import { Icon } from './Icon'
import { RoutingInspector, TurnNotes, TurnPending } from './RoutingInspector'
import { useRestoredTurn } from '@/hooks/useRestoredTurn'
import type { ChatMessage } from '@/hooks/useChatStream'
import { agentIdentity, dateTime, num } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { handoffReason } from '@/lib/handoff'
import { href } from '@/lib/route'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type { Ticket, TicketDetail } from '@/api/types'

const STATUSES = ['open', 'assigned', 'resolved', 'cancelled'] as const

const STATUS_LABEL: Record<string, Key> = {
  open: 'tk.status.open',
  assigned: 'tk.status.assigned',
  resolved: 'tk.status.resolved',
  cancelled: 'tk.status.cancelled',
}

const STATUS_TONE: Record<string, Tone> = {
  open: 'caution', assigned: 'info', resolved: 'success', cancelled: 'neutral',
}

const ACTION_LABEL: Record<string, Key> = {
  assigned: 'tk.to.assigned',
  resolved: 'tk.to.resolved',
  cancelled: 'tk.to.cancelled',
  open: 'tk.to.open',
}

/** 没有后续流转的状态：进了就出不来，所以进之前要确认一次。 */
const FINAL = new Set(['resolved', 'cancelled'])

export function StatusChip({ status }: { status: string }) {
  const { t } = useT()
  const key = STATUS_LABEL[status]
  return <Chip tone={STATUS_TONE[status] ?? 'neutral'} dot>{key ? t(key) : status}</Chip>
}

/**
 * 工单。数据全部来自 /tickets：编号、问题、状态、创建时间。
 *
 * 系统里**没有**负责人、响应时限这些字段，所以界面上也没有——"已分配"只是
 * 一个状态，不代表分给了谁。通知同理：只转述投递记录，没记录就说没记录。
 */
export function TicketsPanel({ base, ticketId }: { base: string; ticketId?: string }) {
  const { t, lang } = useT()
  const [status, setStatus] = useState('')
  const list = useTickets(base, status)

  if (ticketId) return <TicketDetailView base={base} ticketId={ticketId} />

  const data = list.data
  const stats = data?.stats ?? {}

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div className="page-heading">
          <div>
            <h2 className="page-title">{t('tk.pageTitle')}</h2>
            <p className="page-lede">{t('tk.lede')}</p>
          </div>
        </div>

        {data?.notifications_enabled === false && (
          <p className="note neutral">{t('tk.notify.off')}</p>
        )}

        <div className="tabs" role="tablist" aria-label={t('tk.filter')}>
          {['', ...STATUSES].map((s) => (
            <button key={s || 'all'} role="tab" aria-selected={status === s}
                    className={`tab${status === s ? ' is-active' : ''}`} onClick={() => setStatus(s)}>
              {s ? t(STATUS_LABEL[s]!) : t('tk.filter.all')}
              {data && <span className="tab-count">{s ? stats[s] ?? 0 : stats.total ?? 0}</span>}
            </button>
          ))}
        </div>

        {list.isError ? (
          (() => {
            const { body, detail } = explainError(list.error, t)
            return (
              <StateBlock tone="danger" title={t('tk.error.title')} detail={detail}
                action={<button className="btn btn-secondary" onClick={() => list.refetch()}>{t('kb.retry')}</button>}>
                {body}
              </StateBlock>
            )
          })()
        ) : !data ? (
          <StateBlock busy title={t('tk.loading')} />
        ) : data.tickets.length === 0 ? (
          <StateBlock title={status
            ? t('tk.empty.filtered', { status: t(STATUS_LABEL[status]!) })
            : t('tk.empty')}>
            {status ? undefined : t('tk.empty.body')}
          </StateBlock>
        ) : (
          <>
          {/* 接口一次最多给 200 张。没显示全的时候要说出来，不然会被当成"只有这些" */}
          {data.tickets.length < (status ? stats[status] ?? 0 : stats.total ?? 0) && (
            <p className="list-summary">
              {t('tk.truncated', { n: data.tickets.length, total: status ? stats[status]! : stats.total! })}
            </p>
          )}
          <div className="table-scroll">
            <table className="data ticket-table">
              <thead>
                <tr>
                  <th scope="col">{t('tk.col.id')}</th>
                  <th scope="col">{t('tk.col.summary')}</th>
                  <th scope="col">{t('tk.col.status')}</th>
                  <th scope="col">{t('tk.col.created')}</th>
                </tr>
              </thead>
              <tbody>
                {data.tickets.map((ticket) => (
                  <tr key={ticket.id}>
                    <td><a className="mono" href={href.ticket(ticket.id)}>{ticket.id}</a></td>
                    <td className="ticket-summary">
                      <a href={href.ticket(ticket.id)} tabIndex={-1}>
                        {ticket.last_message || t('tk.noSummary')}
                      </a>
                    </td>
                    <td><StatusChip status={ticket.status} /></td>
                    <td className="n">{dateTime(ticket.created_at, lang)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </>
        )}
      </div>
    </div>
  )
}

/** 通知那一栏：只转述 outbox 里的投递记录。 */
function notificationText(
  ticket: TicketDetail, t: (k: Key, v?: Record<string, string | number>) => string, lang: 'en' | 'zh',
): string {
  const n = ticket.notification
  if (n === undefined) return t('tk.notify.unknown')
  if (n === null) {
    return ticket.notifications_enabled === false ? t('tk.notify.none') : t('tk.notify.noRecord')
  }
  if (n.status === 'delivered') return t('tk.notify.delivered', { date: dateTime(n.updated_at, lang) })
  if (n.status === 'failed') return t('tk.notify.failed', { n: n.attempts })
  return t('tk.notify.pending', { n: n.attempts })
}

/**
 * 转人工那一轮的经过：用工单上的 turn_id 从事件日志取回，原样交给编排检查器。
 * 点了才取；超出保留期、或者当时日志没开，照实说。老工单没有 turn_id，这一节不出现。
 */
function TicketTurn({ base, turnId }: { base: string; turnId: string }) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const message: ChatMessage = { id: turnId, role: 'assistant', content: '', restored: true, turnId }
  const turn = useRestoredTurn(base, message, open)
  return (
    <section className="ticket-section" aria-labelledby="tk-turn">
      <h3 id="tk-turn">{t('tk.turn')}</h3>
      <p className="field-hint">{t('tk.turn.note')}</p>
      {!open ? (
        <button className="btn btn-secondary" onClick={() => setOpen(true)}>{t('tk.turn.show')}</button>
      ) : turn.state === 'ready' ? (
        <div className="ticket-turn">
          <TurnNotes redacted={turn.redacted} complete={turn.complete} abandoned={turn.abandoned} />
          <RoutingInspector message={turn.message} />
        </div>
      ) : <TurnPending turn={turn} />}
    </section>
  )
}

function TicketDetailView({ base, ticketId }: { base: string; ticketId: string }) {
  const { t, lang } = useT()
  const { data: ticket, error, isError, refetch } = useTicket(base, ticketId)
  const update = useSetTicketStatus(base)
  const [confirming, setConfirming] = useState<string | null>(null)

  // 键盘操作时焦点的去向。状态按钮一按就会被换掉（出确认、或者状态变了可选的
  // 下一步跟着变），焦点留在被换掉的按钮上会掉回 <body>：看不到焦点在哪，读屏
  // 也不会念出刚出现的确认提示。
  //   · 出确认 → 焦点落在"保持不变"上（不可撤销的操作，默认停在安全的那个）
  //   · 反悔（按钮或 Esc）→ 回到刚才按的那个状态按钮
  //   · 提交 → 落在这一节的标题上，新状态或失败提示就在它下面
  const statusHeading = useRef<HTMLHeadingElement>(null)
  const actions = useRef<HTMLDivElement>(null)
  const keep = useRef<HTMLButtonElement>(null)
  const returnTo = useRef<string | null>(null)
  useEffect(() => {
    if (confirming) keep.current?.focus()
    else if (returnTo.current) {
      actions.current?.querySelector<HTMLButtonElement>(`[data-next="${returnTo.current}"]`)?.focus()
      returnTo.current = null
    }
  }, [confirming])

  const back = (
    <a className="back-link" href={href.tickets()}>
      <Icon name="arrow" size={16} />{t('tk.back')}
    </a>
  )

  if (isError) {
    const { body, detail } = explainError(error, t, 'tk.missing')
    return (
      <div className="page scroll"><div className="page-inner reader">
        {back}
        <StateBlock tone="danger" title={t('tk.error.title')} detail={detail}
          action={<button className="btn btn-secondary" onClick={() => refetch()}>{t('kb.retry')}</button>}>
          {body}
        </StateBlock>
      </div></div>
    )
  }
  if (!ticket) {
    return (
      <div className="page scroll"><div className="page-inner reader">
        {back}
        <StateBlock busy title={t('tk.loading')} />
      </div></div>
    )
  }

  const transitions = ticket.allowed_transitions
  const change = (status: string) => {
    setConfirming(null)
    statusHeading.current?.focus()
    update.mutate({ ticketId: ticket.id, status })
  }
  const cancelConfirm = () => {
    returnTo.current = confirming
    setConfirming(null)
  }
  const reason = handoffReason(ticket.reason, t, lang)

  return (
    <div className="page scroll">
      <div className="page-inner reader">
        {back}
        <header className="reader-head">
          <h2 className="page-title">{t('tk.title')} <span className="mono">{ticket.id}</span></h2>
          <StatusChip status={ticket.status} />
        </header>

        <section className="ticket-section" aria-labelledby="tk-request">
          <h3 id="tk-request">{t('tk.request')}</h3>
          <p className="ticket-quote">{ticket.last_message || t('tk.noSummary')}</p>
        </section>

        <section className="ticket-section" aria-labelledby="tk-reason">
          <h3 id="tk-reason">{t('tk.reason')}</h3>
          <p className="ticket-quote">{reason}</p>
          <p className="field-hint">{t('tk.reason.note')}</p>
        </section>

        {ticket.turn_id && <TicketTurn base={base} turnId={ticket.turn_id} />}

        <section className="ticket-section" aria-labelledby="tk-status">
          <h3 id="tk-status" ref={statusHeading} tabIndex={-1}>{t('tk.status.title')}</h3>
          <dl className="reader-meta">
            <div><dt>{t('tk.status.title')}</dt><dd><StatusChip status={ticket.status} /></dd></div>
            <div><dt>{t('tk.created')}</dt><dd>{dateTime(ticket.created_at, lang)}</dd></div>
            <div><dt>{t('tk.updated')}</dt><dd>{dateTime(ticket.updated_at, lang)}</dd></div>
            <div><dt>{t('tk.notification')}</dt><dd>{notificationText(ticket, t, lang)}</dd></div>
          </dl>
          {ticket.notification?.status === 'failed' && ticket.notification.last_error && (
            <p className="field-hint mono">{ticket.notification.last_error}</p>
          )}

          {transitions === undefined ? (
            <p className="field-hint">{t('tk.noTransitions')}</p>
          ) : transitions.length === 0 ? (
            <p className="field-hint">{t('tk.final')}</p>
          ) : confirming ? (
            <div className="ticket-confirm" role="group" aria-label={t(ACTION_LABEL[confirming]!)}
                 aria-describedby="tk-confirm-warning"
                 onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); cancelConfirm() } }}>
              <p id="tk-confirm-warning">{t('tk.confirm.final', { status: t(STATUS_LABEL[confirming]!) })}</p>
              <button className="btn btn-primary" onClick={() => change(confirming)}>{t('tk.confirm.yes')}</button>
              <button ref={keep} className="btn btn-secondary" onClick={cancelConfirm}>{t('tk.confirm.no')}</button>
            </div>
          ) : (
            <div className="ticket-actions" ref={actions}>
              {transitions.map((next) => (
                <button key={next} data-next={next} className="btn btn-secondary" disabled={update.isPending}
                        aria-busy={update.isPending && update.variables?.status === next}
                        onClick={() => (FINAL.has(next) ? setConfirming(next) : change(next))}>
                  {t(ACTION_LABEL[next] ?? 'tk.to.open')}
                </button>
              ))}
            </div>
          )}
          {/* 改状态失败：工单还是原来的状态（上面的 chip 没变），把后端的原因说出来 */}
          {update.isError && (
            <p className="note danger" role="alert">
              <strong>{t('tk.update.failed')}</strong>{' '}
              {update.error instanceof ApiError && update.error.status === 422
                ? t('tk.update.rejected') : explainError(update.error, t).body}
              <span className="mono state-detail"> {update.error instanceof Error ? update.error.message : ''}</span>
            </p>
          )}
          <p className="field-hint">{t('tk.status.note')}</p>
        </section>

        <Transcript base={base} ticket={ticket} />

        <details className="raw">
          <summary>{t('tk.more')}</summary>
          <dl className="reader-meta">
            <div><dt>{t('tk.intent')}</dt><dd className="mono">{ticket.intent || '—'}</dd></div>
            <div><dt>{t('tk.confidence')}</dt><dd className="mono">{num(ticket.answer_confidence)}</dd></div>
            <div><dt>{t('tk.requester')}</dt><dd className="mono">{ticket.user_id}</dd></div>
            <div><dt>{t('tk.conv')}</dt><dd className="mono">{ticket.conv_id}</dd></div>
            {ticket.request_id && <div><dt>{t('tk.requestId')}</dt><dd className="mono">{ticket.request_id}</dd></div>}
            {/* 上面那句是对译过的；原始记录留在这里，对账时能对上后端日志 */}
            {reason !== ticket.reason && <div><dt>{t('tk.reason.raw')}</dt><dd className="mono">{ticket.reason}</dd></div>}
          </dl>
        </details>
      </div>
    </div>
  )
}

/** 关联对话：从审计日志取回这张工单所在会话的原文。 */
function Transcript({ base, ticket }: { base: string; ticket: Ticket }) {
  const { t, lang } = useT()
  const conv = useConversation(base, ticket.conv_id)
  return (
    <section className="ticket-section" aria-labelledby="tk-transcript">
      <h3 id="tk-transcript">{t('tk.transcript')}</h3>
      {conv.isError ? (
        (() => {
          const { body, detail } = explainError(conv.error, t, 'tk.transcript.missing')
          return <StateBlock tone="danger" title={t('tk.transcript.error')} detail={detail}>{body}</StateBlock>
        })()
      ) : !conv.data ? (
        <StateBlock busy title={t('tk.transcript.loading')} />
      ) : (
        <ol className="transcript">
          {[...conv.data.messages].sort((a, b) => a.seq - b.seq).map((m) => (
            <li key={m.seq} className={m.role === 'user' ? 'from-user' : 'from-assistant'}>
              <p className="transcript-head">
                <strong>
                  {m.role === 'user' ? t('tk.role.user')
                    : m.agent_type ? agentIdentity(m.agent_type, t).name : t('tk.role.assistant')}
                </strong>
                <span>{dateTime(m.created_at, lang)}</span>
                {m.role === 'user' && m.request_id && m.request_id === ticket.request_id && (
                  <Chip>{t('tk.transcript.this')}</Chip>
                )}
              </p>
              {m.role === 'user'
                ? <p className="transcript-text">{m.content}</p>
                : <div className="prose-chat transcript-text"><Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown></div>}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
