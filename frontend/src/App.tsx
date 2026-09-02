import { useCallback, useMemo, useState } from 'react'
import { loadSettings, saveSettings, type Settings } from '@/api/client'
import { useChatStream, type ChatMessage } from '@/hooks/useChatStream'
import { useTheme } from '@/hooks/useTheme'
import { ChatPanel } from '@/components/ChatPanel'
import { EvalPanel } from '@/components/EvalPanel'
import { KnowledgePanel } from '@/components/KnowledgePanel'
import { MonitorPanel } from '@/components/MonitorPanel'
import { RoutingInspector } from '@/components/RoutingInspector'
import { Sidebar, type View } from '@/components/Sidebar'
import { Icon } from '@/components/Icon'

const TITLES: Record<View, string> = {
  chat: '对话工作台',
  monitor: '在线监控',
  eval: '端到端评测',
  knowledge: '知识库',
}

const INSPECTOR_KEY = 'helpdesk.inspector'

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [streaming, setStreaming] = useState(true)
  // 讲解模式：默认关（保持干净的产品姿态），打开后每轮摊开完整编排解释
  const [explain, setExplain] = useState(() => localStorage.getItem('helpdesk.explain') === 'on')
  const [view, setView] = useState<View>('chat')
  const [pinned, setPinned] = useState<string | null>(null)
  const { theme, toggle } = useTheme()

  // 窄屏首次进入默认收起：检查器是浮层，开着等于盖住整个对话区
  const [inspectorOpen, setInspectorOpen] = useState(() => {
    const saved = localStorage.getItem(INSPECTOR_KEY)
    return saved ? saved === 'open' : window.innerWidth > 1180
  })

  const setInspector = useCallback((open: boolean) => {
    setInspectorOpen(open)
    localStorage.setItem(INSPECTOR_KEY, open ? 'open' : 'closed')
  }, [])

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

  // 检查器默认跟最近一条助手消息；点某一轮的检查按钮后钉在那一轮
  const inspected = useMemo<ChatMessage | undefined>(() => {
    if (pinned) return messages.find((m) => m.id === pinned)
    return [...messages].reverse().find((m) => m.role === 'assistant')
  }, [messages, pinned])

  const isChat = view === 'chat'

  return (
    <div className="app" data-inspector={isChat && inspectorOpen ? 'open' : 'closed'}>
      <Sidebar
        settings={settings} onChange={updateSettings}
        view={view} onView={setView}
        onNewChat={() => { clear(); setPinned(null); setView('chat') }}
        theme={theme} onToggleTheme={toggle}
      />

      <main className="main">
        <header className="topbar">
          <h1>{TITLES[view]}</h1>
          <span className="spacer" />
          {isChat && (
            <>
              <label className="switch">
                <input type="checkbox" checked={streaming} onChange={(e) => setStreaming(e.target.checked)} />
                <span className="track" />流式
              </label>
              <label className="switch" title="展开每轮的完整编排解释">
                <input
                  type="checkbox" checked={explain}
                  onChange={(e) => {
                    setExplain(e.target.checked)
                    localStorage.setItem('helpdesk.explain', e.target.checked ? 'on' : 'off')
                  }}
                />
                <span className="track" />讲解
              </label>
              <button className="icon-btn" title="清空对话" aria-label="清空对话"
                      onClick={() => { clear(); setPinned(null) }}>
                <Icon name="refresh" />
              </button>
              <button
                className="icon-btn" aria-pressed={inspectorOpen}
                title="路由检查器" aria-label="切换路由检查器"
                onClick={() => setInspector(!inspectorOpen)}
              >
                <Icon name="panel" />
              </button>
            </>
          )}
        </header>

        {isChat && (
          <ChatPanel
            messages={messages} busy={busy} explain={explain}
            onSend={(text) => { setPinned(null); send(text, streaming) }}
            onInspect={(m) => { setPinned(m.id); setInspector(true) }}
          />
        )}
        {view === 'monitor' && <MonitorPanel base={settings.apiBaseUrl} />}
        {view === 'eval' && <EvalPanel base={settings.apiBaseUrl} />}
        {view === 'knowledge' && <KnowledgePanel base={settings.apiBaseUrl} />}
      </main>

      {isChat && (
        <aside className="inspector" aria-label="路由检查器">
          <div className="inspector-head">
            <h2>路由检查器</h2>
            <span className="spacer" />
            <button className="icon-btn" aria-label="关闭检查器" onClick={() => setInspector(false)}>
              <Icon name="close" />
            </button>
          </div>
          <div className="inspector-body scroll">
            <RoutingInspector message={inspected} />
          </div>
        </aside>
      )}
    </div>
  )
}
