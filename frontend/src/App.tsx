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

  // 服务端已不认这个 conv_id（404）：清掉，别让下一条消息顶着一个假的历史 id 发出去
  const onConversationLost = useCallback(() => updateSettings({ conversationId: '' }), [updateSettings])

  const { messages, busy, restoring, send, clear } =
    useChatStream(settings, onConversationId, onConversationLost)

  // 「新建对话」和「清空对话」都要连 conv_id 一起清。只清屏幕不清 id，
  // 服务端记忆还在，下一条消息接着上一个会话答——操作员以为是新客户，模型不这么认为。
  const startNewChat = useCallback(() => {
    clear()
    setPinned(null)
    updateSettings({ conversationId: '' })
  }, [clear, updateSettings])

  // 检查器默认跟最近一条助手消息；点某一轮的检查按钮后钉在那一轮。
  //
  // 真实后端一轮要 8~21s，而本轮的 routing 事件到达之前这条助手消息上什么都没有，
  // 直接跟最新一条就等于「一发消息，检查器整块清空、空到 routing 到达为止」——
  // 演示夹具瞬间返回时这一下闪不出来，接了真后端就是十几秒的空面板，
  // 而这十几秒恰恰是最想回头看上一轮依据的时候。所以本轮还没有任何决策数据时
  // 继续显示上一轮，并在面板上标出「这是上一轮」。
  const inspected = useMemo<ChatMessage | undefined>(() => {
    if (pinned) return messages.find((m) => m.id === pinned)
    const assistants = messages.filter((m) => m.role === 'assistant')
    const latest = assistants[assistants.length - 1]
    if (!latest || latest.routing || latest.result || latest.error) return latest
    return [...assistants].reverse().find((m) => m.routing || m.result) ?? latest
  }, [messages, pinned])

  // 上面那个回退生效时，面板里的数字属于上一轮，必须说出来——否则读成本轮就是误导
  const inspectedIsStale = !pinned && !!inspected && inspected !== messages[messages.length - 1]

  const isChat = view === 'chat'

  return (
    <div className="app" data-inspector={isChat && inspectorOpen ? 'open' : 'closed'}>
      <Sidebar
        settings={settings} onChange={updateSettings}
        view={view} onView={setView}
        onNewChat={() => { startNewChat(); setView('chat') }}
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
                      onClick={startNewChat}>
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
            messages={messages} busy={busy} explain={explain} restoring={restoring}
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
            {inspectedIsStale && (
              <p className="note caution" style={{ marginBottom: 12 }}>
                本轮仍在编排，下面是上一轮的决策。
              </p>
            )}
            <RoutingInspector message={inspected} />
          </div>
        </aside>
      )}
    </div>
  )
}
