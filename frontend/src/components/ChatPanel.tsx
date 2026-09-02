import { useEffect, useRef, useState } from 'react'
import { MessageBubble } from './MessageBubble'
import { Icon } from './Icon'
import type { ChatMessage } from '@/hooks/useChatStream'

/** 内置示例问题，覆盖规则快通道、跨域主辅协作、答案策略拦截三条典型链路。 */
const SAMPLES = [
  '12 号下的那单现在生产到哪一步了？',
  '橱柜门板到货的时候有磕碰，能补发吗？',
  '想把吊柜高度改一下，另外尾款发票什么时候能开？',
  '石英石台面多少钱一米？',
]

export function ChatPanel({ messages, busy, onSend, onInspect, explain }: {
  messages: ChatMessage[]
  busy: boolean
  onSend: (text: string) => void
  onInspect: (m: ChatMessage) => void
  explain?: boolean
}) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const areaRef = useRef<HTMLTextAreaElement>(null)

  // 新增量到达时贴底滚动
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages])

  const grow = () => {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }

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
        {messages.length === 0 ? (
          <div className="thread-empty">
            <h2>今天要处理哪一单？</h2>
            <p>发一条客户问题，右侧检查器会摊开这一轮的完整编排决策。</p>
            <div className="suggestions">
              {SAMPLES.map((s) => (
                <button key={s} className="suggestion" onClick={() => onSend(s)}>{s}</button>
              ))}
            </div>
          </div>
        ) : (
          <div className="thread-inner">
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} explain={explain} onInspect={() => onInspect(m)} />
            ))}
          </div>
        )}
      </div>

      <div className="composer-wrap">
        <form className="composer" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <label className="sr-only" htmlFor="draft">输入客户问题</label>
          <textarea
            id="draft" ref={areaRef} rows={1} value={draft}
            placeholder="输入客户问题，Enter 发送 / Shift+Enter 换行"
            onChange={(e) => { setDraft(e.target.value); grow() }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
            }}
          />
          <div className="composer-bar">
            <button className="send" type="submit" disabled={busy || !draft.trim()} aria-label="发送">
              <Icon name="send" size={18} />
            </button>
          </div>
        </form>
        <p className="composer-hint">
          回复由多 Agent 编排生成，价格与交期承诺经答案策略校验后才会出站。
        </p>
      </div>
    </>
  )
}
