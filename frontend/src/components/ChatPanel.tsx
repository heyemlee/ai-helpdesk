import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { MessageBubble } from './MessageBubble'
import { Icon, type IconName } from './Icon'
import type { ChatMessage } from '@/hooks/useChatStream'
import type { HealthResponse, RoutingConfig } from '@/api/types'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'

/**
 * 示例问题：知识问答、订单查询两类各有代表。按钮上是一行短标签，点一下把完整问题
 * 填进输入框，不直接发——订单那条带着占位的单号，本来就得先改成真的，所以填进去后
 * 把占位选中，直接打单号就替换掉。
 *
 * 订单那条只在业务数据接上时给。后端明说没接（客户实例那种）就不给一个注定查不到的例子；
 * 没报（老版本）照常给，和「可以查询什么」里只说"没报"一个口径，不替它猜。
 */
const SAMPLES: Array<{ question: Key; label: Key; icon: IconName; needsData?: boolean }> = [
  { question: 'cw.sample.1', label: 'cw.chip.1', icon: 'pulse', needsData: true },
  { question: 'cw.sample.3', label: 'cw.chip.3', icon: 'arrow' },
  { question: 'cw.sample.4', label: 'cw.chip.4', icon: 'book' },
  { question: 'cw.sample.2', label: 'cw.chip.2', icon: 'checklist' },
]

/**
 * 「可以查询什么」：三类问题各自从哪里得到答案。
 *
 * 订单数据那一条照 /health 的 capabilities 说话——这个工作台接没接业务数据
 * 是部署决定的，写死"可以查订单"的话，没接的那套环境里员工会白等一个查不到的答案。
 * 后端没报（老版本）就说没报，不替它猜。
 */
export function ScopeGuide({ capabilities }: { capabilities?: HealthResponse['capabilities'] }) {
  const { t } = useT()
  const dataKey: Key = capabilities === undefined ? 'cw.scope.data.unknown'
    : capabilities.business_data ? 'cw.scope.data.on' : 'cw.scope.data.off'
  return (
    <div className="scope">
      <dl className="scope-list">
        <div className="scope-item">
          <dt><Icon name="book" size={16} />{t('cw.scope.kb.title')}</dt>
          <dd>{t('cw.scope.kb.body')}</dd>
        </div>
        <div className="scope-item">
          <dt><Icon name="pulse" size={16} />{t('cw.scope.data.title')}</dt>
          <dd>{t(dataKey)}</dd>
        </div>
        <div className="scope-item">
          <dt><Icon name="person" size={16} />{t('cw.scope.human.title')}</dt>
          <dd>{t('cw.scope.human.body')}</dd>
        </div>
      </dl>
      <p className="scope-limits"><Icon name="shield" size={14} />{t('cw.scope.limits')}</p>
    </div>
  )
}

export function ChatPanel({ messages, busy, onSend, onInspect, explain, restoring, capabilities, routingConfig, base }: {
  messages: ChatMessage[]
  busy: boolean
  onSend: (text: string) => void
  onInspect: (m: ChatMessage) => void
  explain?: boolean
  /** 正在从服务端拉刷新前的历史 */
  restoring?: boolean
  capabilities?: HealthResponse['capabilities']
  routingConfig?: RoutingConfig
  base?: string
}) {
  const { t } = useT()
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const areaRef = useRef<HTMLTextAreaElement>(null)
  const followLatest = useRef(true)
  const [awayFromBottom, setAwayFromBottom] = useState(false)

  // Follow streaming output only while the reader remains near the latest reply.
  useEffect(() => {
    if (followLatest.current) listRef.current?.scrollTo({ top: messages.length ? listRef.current.scrollHeight : 0 })
  }, [messages, explain])

  const grow = () => {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }

  const restoredCount = messages.filter((m) => m.restored).length

  // 空对话时输入框居中，标题在上、例子在下；发出第一条就去掉，输入框落回底部。
  // 两种状态用的是同一个输入框，只换样式——换成两处各渲染一个的话，
  // 发出第一条那一下 textarea 重新挂载，焦点就丢了。
  const empty = messages.length === 0 && !restoring
  const samples = SAMPLES.filter((s) => !s.needsData || capabilities?.business_data !== false)

  const fill = (question: Key) => {
    const text = t(question)
    // 先同步提交新值，再在同一个点击里选中占位
    flushSync(() => setDraft(text))
    const el = areaRef.current
    if (!el) return
    el.focus()
    const slot = t('cw.sample.slot')
    const at = text.indexOf(slot)
    if (at >= 0) el.setSelectionRange(at, at + slot.length)
    else el.setSelectionRange(text.length, text.length)
    grow()
  }

  const submit = () => {
    const text = draft.trim()
    if (!text || busy || restoring) return
    followLatest.current = true
    setAwayFromBottom(false)
    onSend(text)
    setDraft('')
    requestAnimationFrame(grow)
  }

  return (
    <>
      <div className={empty ? 'thread scroll is-empty' : 'thread scroll'} ref={listRef} onScroll={() => {
        const el = listRef.current!
        const away = el.scrollHeight - el.scrollTop - el.clientHeight > 100
        followLatest.current = !away
        setAwayFromBottom(away)
      }}>
        {messages.length === 0 && restoring ? (
          // 拉历史的那一两秒别闪一下空态——空态会被读成「这是个新会话」
          <div className="thread-inner">
            <div className="thinking"><span className="pulse" />{t('cw.restoring')}</div>
          </div>
        ) : messages.length === 0 ? (
          // 「可以查询什么」的完整说明只留在顶栏的弹层里：第一屏只说一句，
          // 依据和工单的细节等回复里真出现时再讲（依据列表、工单卡下面都有）
          <div className="thread-empty">
            <h2>{t('cw.empty.title')}</h2>
            <p>{t('cw.empty.body')}</p>
          </div>
        ) : (
          <div className="thread-inner">
            {restoredCount > 0 && (
              <p className="note neutral" style={{ marginBottom: 16 }}>
                {t('cw.restored', { n: restoredCount })}
              </p>
            )}
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} explain={explain} base={base}
                             routingConfig={routingConfig} onInspect={() => onInspect(m)} />
            ))}
          </div>
        )}
      </div>

      <div className={empty ? 'composer-wrap is-empty' : 'composer-wrap'}>
        {awayFromBottom && messages.length > 0 && <button className="btn btn-secondary latest-message" onClick={() => {
          followLatest.current = true
          setAwayFromBottom(false)
          listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
        }}>{t('cw.latest')} ↓</button>}
        <form className="composer" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <label className="sr-only" htmlFor="draft">{t('cw.composeLabel')}</label>
          <textarea
            id="draft" ref={areaRef} rows={1} value={draft} disabled={restoring}
            placeholder={t('cw.placeholder')}
            onChange={(e) => { setDraft(e.target.value); grow() }}
            onKeyDown={(e) => {
              // 输入法组合中的 Enter 是「确认候选」，不是「发送」。少了 isComposing
              // 这一判，中文用户每打完一个词按 Enter 上屏就会把半截问题发出去——
              // 而且是真发：一整轮编排、几秒 LLM、可能还建一张工单。
              // 英文输入和粘贴走不到组合态，所以只有中文/日文/韩文用户会踩到。
              if (e.nativeEvent.isComposing) return
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
            }}
          />
          <div className="composer-bar">
            <span className="composer-keyboard">{busy ? t('cw.busy') : t('cw.keyboard')}</span>
            <button className="send" type="submit" disabled={busy || restoring || !draft.trim()} aria-label={t('cw.send')}>
              <Icon name="send" size={18} />
            </button>
          </div>
        </form>
        {empty && (
          <div className="suggestions starters" role="group" aria-label={t('cw.examples')}>
            {samples.map((sample) => (
              <button key={sample.question} type="button" className="suggestion" onClick={() => fill(sample.question)}>
                <Icon name={sample.icon} size={14} />{t(sample.label)}
              </button>
            ))}
          </div>
        )}
        <p className="composer-hint">
          <Icon name="shield" size={12} />{t('cw.hint')}
        </p>
      </div>
    </>
  )
}
