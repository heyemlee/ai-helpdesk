import { useCallback, useMemo, useState } from 'react'
import { loadSettings, saveSettings, type Settings } from '@/api/client'
import { useChatStream } from '@/hooks/useChatStream'
import { ChatPanel } from '@/components/ChatPanel'
import { EvalPanel } from '@/components/EvalPanel'
import { KnowledgePanel } from '@/components/KnowledgePanel'
import { MonitorPanel } from '@/components/MonitorPanel'
import { RoutingInspector } from '@/components/RoutingInspector'
import { Sidebar } from '@/components/Sidebar'

type Tab = 'routing' | 'monitor' | 'eval' | 'knowledge'

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'routing', label: '路由' },
  { id: 'monitor', label: '监控' },
  { id: 'eval', label: '评测' },
  { id: 'knowledge', label: '知识库' },
]

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [streaming, setStreaming] = useState(true)
  const [tab, setTab] = useState<Tab>('routing')

  const updateSettings = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch }
      saveSettings(next)
      return next
    })
  }, [])

  const onConversationId = useCallback((id: string) => {
    setSettings((prev) => {
      if (prev.conversationId || !id) return prev
      const next = { ...prev, conversationId: id }
      saveSettings(next)
      return next
    })
  }, [])

  const { messages, busy, send, clear } = useChatStream(settings, onConversationId)

  // 路由检查器只看最近一条助手消息
  const lastAssistant = useMemo(
    () => [...messages].reverse().find((m) => m.role === 'assistant'),
    [messages],
  )

  return (
    <div className="flex h-screen bg-slate-950 text-slate-100">
      <Sidebar settings={settings} onChange={updateSettings} />

      <main className="flex min-w-0 flex-1 gap-4 p-4">
        <div className="flex min-w-0 flex-1 flex-col">
          <ChatPanel
            messages={messages}
            busy={busy}
            streaming={streaming}
            onStreamingChange={setStreaming}
            onSend={(text) => send(text, streaming)}
            onClear={clear}
          />
        </div>

        <div className="flex w-[26rem] shrink-0 flex-col gap-3">
          <nav className="flex gap-1 rounded-lg bg-slate-900/60 p-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition ${
                  tab === t.id
                    ? 'bg-slate-700/70 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {tab === 'routing' && <RoutingInspector message={lastAssistant} />}
            {tab === 'monitor' && <MonitorPanel base={settings.apiBaseUrl} />}
            {tab === 'eval' && <EvalPanel base={settings.apiBaseUrl} />}
            {tab === 'knowledge' && <KnowledgePanel base={settings.apiBaseUrl} />}
          </div>
        </div>
      </main>
    </div>
  )
}
