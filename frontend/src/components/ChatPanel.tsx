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

export function ChatPanel({ messages, busy, onSend, onInspect, explain, restoring }: {
  messages: ChatMessage[]
  busy: boolean
  onSend: (text: string) => void
  onInspect: (m: ChatMessage) => void
  explain?: boolean
  /** 正在从服务端拉刷新前的历史 */
  restoring?: boolean
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
            <div className="thinking"><span className="pulse" />正在恢复会话…</div>
          </div>
        ) : messages.length === 0 ? (
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
            {restoredCount > 0 && (
              <p className="note neutral" style={{ marginBottom: 16 }}>
                已从服务端恢复 {restoredCount} 条历史消息。路由决策只在当轮保留，
                检查器只能查本次新发的消息。
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
          <label className="sr-only" htmlFor="draft">输入客户问题</label>
          <textarea
            id="draft" ref={areaRef} rows={1} value={draft}
            placeholder="输入客户问题，Enter 发送 / Shift+Enter 换行"
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
