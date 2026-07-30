import { useCallback, useMemo, useState } from 'react'
import { loadSettings, saveSettings, type Settings } from '@/api/client'
import { useOrchestratorConfig } from '@/api/hooks'
import { useT } from '@/i18n'
import { useChatStream, type ChatMessage } from '@/hooks/useChatStream'
import { useTheme } from '@/hooks/useTheme'
import { ChatPanel } from '@/components/ChatPanel'
import { EvalPanel } from '@/components/EvalPanel'
import { KnowledgePanel } from '@/components/KnowledgePanel'
import { MonitorPanel } from '@/components/MonitorPanel'
import { RoutingInspector } from '@/components/RoutingInspector'
import { Sidebar, type View } from '@/components/Sidebar'
import { InspectorDrawer } from '@/components/InspectorDrawer'
import { Icon } from '@/components/Icon'

const TITLE_KEYS: Record<View, 'nav.chat' | 'nav.monitor' | 'nav.eval' | 'nav.knowledge'> = {
  chat: 'nav.chat',
  monitor: 'nav.monitor',
  eval: 'nav.eval',
  knowledge: 'nav.knowledge',
}

const INSPECTOR_KEY = 'helpdesk.inspector'

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [streaming, setStreaming] = useState(true)
  // 讲解模式：默认关（保持干净的产品姿态），打开后每轮摊开完整编排解释
  const [explain, setExplain] = useState(() => localStorage.getItem('helpdesk.explain') === 'on')
  const [view, setView] = useState<View>('chat')
  const [chatGeneration, setChatGeneration] = useState(0)
  const [pinned, setPinned] = useState<string | null>(null)
  const { theme, toggle } = useTheme()
  const { t } = useT()

  // 协作门槛的两个常量来自 /monitor 快照；搭 MonitorPanel 已有查询的便车，
  // 不额外轮询。取不到就不传，检查器那条门槛线索性不画。
  // 阈值在 MonitorSummary.routing 里，不在快照顶层。第 20 轮这里写成了顶层字段，
  // 运行时恒为 undefined——门槛线在 React 端一次都没画出来过，而 `tsc --noEmit`
  // 是空转（solution-style tsconfig 要用 `tsc -b`），所以类型检查也没拦住。
  const routingCfg = useOrchestratorConfig(settings.apiBaseUrl).data?.routing
  const thresholds = routingCfg
    ? { minScore: routingCfg.supporting_min_score, minRatio: routingCfg.supporting_min_ratio }
    : undefined

  // Keep the workspace focused; reopen only when the user previously chose it.
  const [inspectorOpen, setInspectorOpen] = useState(() => {
    const saved = localStorage.getItem(INSPECTOR_KEY)
    return saved === 'open'
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
    setChatGeneration((generation) => generation + 1)
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
        settings={settings} onChange={updateSettings} busy={busy || restoring}
        view={view} onView={setView}
        onNewChat={() => { startNewChat(); setView('chat') }}
        theme={theme} onToggleTheme={toggle}
      />

      <main className="main">
        <header className="topbar">
          <div className="topbar-title"><span className="topbar-context">{t('shell.workspace')}</span><h1>{t(TITLE_KEYS[view])}</h1></div>
          <span className="spacer" />
          {isChat && (
            <>
              <button className="btn btn-secondary mobile-new-chat" onClick={startNewChat} disabled={busy || restoring} aria-label={t('shell.newChat')}><Icon name="plus" /></button>
              <details className="chat-options" onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.currentTarget.open = false
                  e.currentTarget.querySelector('summary')?.focus()
                }
              }} onBlur={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.open = false
              }}>
                <summary className="icon-btn" aria-label={t('shell.options')} title={t('shell.options')}><Icon name="settings" /></summary>
                <div className="options-popover">
                  <strong>{t('shell.options')}</strong>
                  <label className="switch">
                    <input type="checkbox" checked={streaming} onChange={(e) => setStreaming(e.target.checked)} />
                    <span className="track" />{t('cw.stream')}
                  </label>
                  <label className="switch" title={t('cw.explain.title')}>
                    <input type="checkbox" checked={explain} onChange={(e) => {
                      setExplain(e.target.checked)
                      localStorage.setItem('helpdesk.explain', e.target.checked ? 'on' : 'off')
                    }} />
                    <span className="track" />{t('cw.explain')}
                  </label>
                </div>
              </details>
              <button
                className="btn btn-secondary inspector-toggle" aria-pressed={inspectorOpen}
                title={t('in.title')} aria-label={t('in.toggle')}
                onClick={() => setInspector(!inspectorOpen)}
              >
                <Icon name="panel" /><span>{t('in.title')}</span>
              </button>
            </>
          )}
        </header>

        <div className="chat-view" hidden={!isChat}>
          <ChatPanel key={chatGeneration}
            messages={messages} busy={busy} explain={explain} restoring={restoring}
            onSend={(text) => { setPinned(null); send(text, streaming) }}
            onInspect={(m) => { setPinned(m.id); setInspector(true) }}
          />
        </div>
        {view === 'monitor' && <MonitorPanel base={settings.apiBaseUrl} />}
        {view === 'eval' && <EvalPanel base={settings.apiBaseUrl} />}
        {view === 'knowledge' && <KnowledgePanel base={settings.apiBaseUrl} />}
      </main>

      <InspectorDrawer open={isChat && inspectorOpen} onClose={() => setInspector(false)}>
          <div className="inspector-body scroll">
            {inspectedIsStale && (
              <p className="note caution" style={{ marginBottom: 12 }}>
                {t('in.stale')}
              </p>
            )}
            <RoutingInspector message={inspected} thresholds={thresholds} />
          </div>
      </InspectorDrawer>
    </div>
  )
}
