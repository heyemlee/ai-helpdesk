import { useEffect, useRef, useState } from 'react'
import { MessageBubble } from './MessageBubble'
import { Badge, Panel } from './ui'
import type { ChatMessage } from '@/hooks/useChatStream'

const SAMPLES = [
  '我要退款，订单号 #12345',
  '我登录不上，而且这个月被重复扣款了',
  '转人工',
  '退款多久能到账？',
]

export function ChatPanel({ messages, busy, streaming, onStreamingChange, onSend, onClear }: {
  messages: ChatMessage[]
  busy: boolean
  streaming: boolean
  onStreamingChange: (v: boolean) => void
  onSend: (text: string) => void
  onClear: () => void
}) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)

  // 新增量到达时贴底滚动
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages])

  const submit = () => {
    const text = draft.trim()
    if (!text || busy) return
    onSend(text)
    setDraft('')
  }

  return (
    <Panel
      title="对话调试"
      badge={<Badge tone="muted">{messages.length} 条</Badge>}
      className="flex min-h-0 flex-1 flex-col [&>div]:flex [&>div]:min-h-0 [&>div]:flex-1 [&>div]:flex-col"
      actions={
        <div className="flex items-center gap-3">
          <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-400">
            <input
              type="checkbox"
              checked={streaming}
              onChange={(e) => onStreamingChange(e.target.checked)}
              className="accent-sky-500"
            />
            流式
          </label>
          <button
            onClick={onClear}
            className="text-[11px] text-slate-500 transition hover:text-slate-300"
          >
            清空
          </button>
        </div>
      }
    >
      <div ref={listRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <p className="text-sm text-slate-400">开始一次客服对话</p>
            <div className="flex flex-wrap justify-center gap-1.5">
              {SAMPLES.map((s) => (
                <button
                  key={s}
                  onClick={() => onSend(s)}
                  className="rounded-full bg-slate-800/70 px-2.5 py-1 text-[11px] text-slate-300
                             ring-1 ring-inset ring-slate-700 transition hover:bg-slate-700/70"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} />)
        )}
      </div>

      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => { e.preventDefault(); submit() }}
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter 发送，Shift+Enter 换行
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
          }}
          rows={2}
          placeholder="输入问题，Enter 发送 / Shift+Enter 换行"
          className="flex-1 resize-none rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2
                     text-sm text-slate-100 outline-none transition placeholder:text-slate-600
                     focus:border-sky-500/50"
        />
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="self-end rounded-lg bg-sky-500 px-4 py-2 text-sm font-medium text-white
                     transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? '…' : '发送'}
        </button>
      </form>
    </Panel>
  )
}
