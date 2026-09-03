import { useEffect, useRef, useState } from 'react'
import { MessageBubble } from './MessageBubble'
import { Icon } from './Icon'
import type { ChatMessage } from '@/hooks/useChatStream'
import { useT } from '@/i18n'

/** 内置示例问题，覆盖规则快通道、跨域主辅协作、答案策略拦截三条典型链路。 */
/* 建议问句跟着界面语言走：夹具的匹配在演示原型里，真实后端不依赖问句语种。 */
const SAMPLE_KEYS = ['cw.sample.1', 'cw.sample.2', 'cw.sample.3', 'cw.sample.4'] as const

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

  // 新增量到达时贴底滚动；讲解模式事后开启会在已有轮次下方展开新内容，同样要贴底
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
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
    if (!text || busy) return
    onSend(text)
    setDraft('')
    requestAnimationFrame(grow)
  }

  return (
    <>
      <div className="thread scroll" ref={listRef}>
        {messages.length === 0 && restoring ? (
          // 拉历史的那一两秒别闪一下空态——空态会被读成「这是个新会话」
          <div className="thread-inner">
            <div className="thinking"><span className="pulse" />{t('cw.restoring')}</div>
          </div>
        ) : messages.length === 0 ? (
          <div className="thread-empty">
            <h2>{t('cw.empty.title')}</h2>
            <p>{t('cw.empty.body')}</p>
            <div className="suggestions">
              {SAMPLE_KEYS.map((k) => (
                <button key={k} className="suggestion" onClick={() => onSend(t(k))}>{t(k)}</button>
              ))}
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
        <form className="composer" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <label className="sr-only" htmlFor="draft">{t('cw.composeLabel')}</label>
          <textarea
            id="draft" ref={areaRef} rows={1} value={draft}
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
            <button className="send" type="submit" disabled={busy || !draft.trim()} aria-label={t('cw.send')}>
              <Icon name="send" size={18} />
            </button>
          </div>
        </form>
        <p className="composer-hint">
          {t('cw.hint')}
        </p>
      </div>
    </>
  )
}
