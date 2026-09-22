import { useEffect, useRef, useState } from 'react'
import { MessageBubble } from './MessageBubble'
import { Icon } from './Icon'
import type { ChatMessage } from '@/hooks/useChatStream'
import { useT } from '@/i18n'

/** 内置示例问题，覆盖规则快通道、跨域主辅协作、答案策略拦截三条典型链路。 */
/* 建议问句跟着界面语言走：夹具的匹配在演示原型里，真实后端不依赖问句语种。 */
const SAMPLES = [
  { question: 'cw.sample.1', title: 'cw.category.1', icon: 'pulse' },
  { question: 'cw.sample.2', title: 'cw.category.2', icon: 'shield' },
  { question: 'cw.sample.3', title: 'cw.category.3', icon: 'checklist' },
  { question: 'cw.sample.4', title: 'cw.category.4', icon: 'book' },
] as const

export function ChatPanel({ messages, busy, onSend, onInspect, explain, restoring }: {
  messages: ChatMessage[]
  busy: boolean
  onSend: (text: string) => void
  onInspect: (m: ChatMessage) => void
  explain?: boolean
  /** 正在从服务端拉刷新前的历史 */
  restoring?: boolean
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
      <div className="thread scroll" ref={listRef} onScroll={() => {
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
          <div className="thread-empty">
            <span className="eyebrow">{t('cw.eyebrow')}</span>
            <h2>{t('cw.empty.title')}</h2>
            <p>{t('cw.empty.body')}</p>
            <div className="starter-section">
              <p className="starter-label">{t('cw.examples')}</p>
              <div className="starter-grid">
                {SAMPLES.map((sample) => (
                  <button key={sample.question} className="starter-card" disabled={busy || restoring}
                    onClick={() => { setDraft(t(sample.question)); areaRef.current?.focus(); requestAnimationFrame(grow) }}>
                    <span className="starter-card-head"><Icon name={sample.icon} /><strong>{t(sample.title)}</strong><Icon name="arrow" size={16} /></span>
                    <span>{t(sample.question)}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="thread-inner">
            {restoredCount > 0 && (
              <p className="note neutral" style={{ marginBottom: 16 }}>
                {t('cw.restored', { n: restoredCount })}
              </p>
            )}
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} explain={explain} onInspect={() => onInspect(m)} />
            ))}
          </div>
        )}
      </div>

      <div className="composer-wrap">
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
            <span className="composer-caption"><Icon name="spark" size={14} />{t('cw.ready')}</span><span className="composer-keyboard">{t('cw.keyboard')}</span>
            <button className="send" type="submit" disabled={busy || restoring || !draft.trim()} aria-label={t('cw.send')}>
              <Icon name="send" size={18} />
            </button>
          </div>
        </form>
        <p className="composer-hint">
          <Icon name="shield" size={12} />{t('cw.hint')}
        </p>
      </div>
    </>
  )
}
