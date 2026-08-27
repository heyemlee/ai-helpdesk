import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { loadSettings, saveSettings, setStaffKey, type Settings } from '@/api/client'
import { useHealth } from '@/api/hooks'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { useChatStream, type ChatMessage } from '@/hooks/useChatStream'
import { useTheme } from '@/hooks/useTheme'
import { href, useRoute, type View } from '@/lib/route'
import { AdminPanel, type ConnectionDraft } from '@/components/AdminPanel'
import { ChatPanel, ScopeGuide } from '@/components/ChatPanel'
import { KnowledgePanel } from '@/components/KnowledgePanel'
import { RoutingInspector } from '@/components/RoutingInspector'
import { Sidebar } from '@/components/Sidebar'
import { TicketsPanel } from '@/components/TicketsPanel'
import { InspectorDrawer } from '@/components/InspectorDrawer'
import { Icon } from '@/components/Icon'

const TITLE_KEYS: Record<View, Key> = {
  chat: 'nav.chat',
  knowledge: 'nav.knowledge',
  tickets: 'nav.tickets',
  admin: 'nav.admin',
}

const INSPECTOR_KEY = 'helpdesk.inspector'
const EXPLAIN_KEY = 'helpdesk.explain'
const STREAM_KEY = 'helpdesk.stream'

/**
 * 员工工作台的外壳。
 *
 * 主导航只有三项——对话、知识库、工单——这是员工每天做的三件事。
 * 监控、评测、编排检查器、模型信息和连接配置都在「管理与诊断」里：
 * 它们还在，但不再是进门就看到的东西。
 *
 * 这里没有"切换成客户视角"之类的开关。谁能看什么是后端判的（员工钥匙），
 * 前端的一个按钮隔离不了任何数据；客户入口是另一套页面和另一组接口的事。
 */
export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [streaming, setStreaming] = useState(() => localStorage.getItem(STREAM_KEY) !== 'off')
  // 学习模式：默认关。打开后每轮讲一遍实际发生了什么，并能打开编排检查器。
  const [explain, setExplain] = useState(() => localStorage.getItem(EXPLAIN_KEY) === 'on')
  const [chatGeneration, setChatGeneration] = useState(0)
  const [pinned, setPinned] = useState<string | null>(null)
  const { theme, toggle } = useTheme()
  const { t } = useT()
  const { route, navigate } = useRoute()
  const queryClient = useQueryClient()

  // 编排阈值和"这个实例接了什么"都在 /health 里；侧栏本来就在轮询它，这里读的是同一份缓存。
  // 取不到就不传：前端不写死后端常量，宁可少讲一个数字。
  const health = useHealth(settings.apiBaseUrl).data
  const routingConfig = health?.routing
  const thresholds = routingConfig
    ? { minScore: routingConfig.supporting_min_score, minRatio: routingConfig.supporting_min_ratio }
    : undefined

  // 检查器只在学习模式里从某一轮打开；上次开着的话保持开着。
  const [inspectorOpen, setInspectorOpen] = useState(
    () => localStorage.getItem(INSPECTOR_KEY) === 'open' && localStorage.getItem(EXPLAIN_KEY) === 'on',
  )

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

  // 这一轮建了工单：侧栏的待处理数和工单列表马上重新取，不等下一次轮询
  const latestTicket = [...messages].reverse().find((m) => m.result?.ticket_id)?.result?.ticket_id
  useEffect(() => {
    if (!latestTicket) return
    void queryClient.invalidateQueries({ queryKey: ['health'] })
    void queryClient.invalidateQueries({ queryKey: ['tickets'] })
  }, [latestTicket, queryClient])

  // 「新建对话」要连 conv_id 一起清。只清屏幕不清 id，服务端记忆还在，
  // 下一条消息接着上一个会话答——操作员以为是新客户，模型不这么认为。
  const startNewChat = useCallback(() => {
    clear()
    setChatGeneration((generation) => generation + 1)
    setPinned(null)
    updateSettings({ conversationId: '' })
    navigate(href.chat())
  }, [clear, updateSettings, navigate])

  const saveConnection = useCallback((draft: ConnectionDraft) => {
    setStaffKey(draft.staffKey)
    setStreaming(draft.streaming)
    localStorage.setItem(STREAM_KEY, draft.streaming ? 'on' : 'off')
    // 换了会话 id：先清屏，恢复历史的那个 effect 只在屏幕上没有消息时才拉
    if (draft.conversationId.trim() !== settings.conversationId) {
      clear()
      setPinned(null)
      setChatGeneration((generation) => generation + 1)
    }
    updateSettings({
      apiBaseUrl: draft.apiBaseUrl.trim() || settings.apiBaseUrl,
      userId: draft.userId.trim() || settings.userId,
      conversationId: draft.conversationId.trim(),
    })
    // 钥匙或地址变了，之前取到的（或没取到的）数据都不算数了
    void queryClient.invalidateQueries()
  }, [clear, queryClient, settings.apiBaseUrl, settings.conversationId, settings.userId, updateSettings])

  // 检查器默认跟最近一条助手消息；点某一轮的检查按钮后钉在那一轮。
  //
  // 真实后端一轮要 8~21s，而本轮的 routing 事件到达之前这条助手消息上什么都没有，
  // 直接跟最新一条就等于「一发消息，检查器整块清空、空到 routing 到达为止」——
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

  const view = route.view
  const isChat = view === 'chat'
  const drawerOpen = isChat && inspectorOpen && explain

  const inspector = (
    <>
      {inspectedIsStale && <p className="note caution" style={{ marginBottom: 12 }}>{t('in.stale')}</p>}
      <RoutingInspector message={inspected} thresholds={thresholds} base={settings.apiBaseUrl} />
    </>
  )

  return (
    <div className="app" data-inspector={drawerOpen ? 'open' : 'closed'}>
      <Sidebar base={settings.apiBaseUrl} view={view} busy={busy || restoring}
               onNewChat={startNewChat} theme={theme} onToggleTheme={toggle} />

      <main className="main">
        <header className="topbar">
          <h1>{t(TITLE_KEYS[view])}</h1>
          <span className="spacer" />
          {isChat && (
            <>
              <details className="chat-options" onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.currentTarget.open = false
                  e.currentTarget.querySelector('summary')?.focus()
                }
              }} onBlur={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.open = false
              }}>
                <summary className="btn btn-ghost scope-toggle">
                  <Icon name="spark" size={16} /><span>{t('cw.scope.open')}</span>
                </summary>
                <div className="options-popover scope-popover">
                  <strong>{t('cw.scope.title')}</strong>
                  <ScopeGuide capabilities={health?.capabilities} />
                </div>
              </details>
              <label className="switch learn-switch" title={t('cw.learn.title')}>
                <input type="checkbox" checked={explain} onChange={(e) => {
                  setExplain(e.target.checked)
                  localStorage.setItem(EXPLAIN_KEY, e.target.checked ? 'on' : 'off')
                  if (!e.target.checked) setInspector(false)
                }} />
                <span className="track" /><span className="switch-text">{t('cw.learn')}</span>
              </label>
              <button className="btn btn-secondary mobile-new-chat" onClick={startNewChat}
                      disabled={busy || restoring} aria-label={t('shell.newChat')}>
                <Icon name="plus" />
              </button>
            </>
          )}
        </header>

        {/* 对话面板切走时只隐藏不卸载：回复还在流的时候去查一眼知识库，回来不能丢 */}
        <div className="chat-view" hidden={!isChat}>
          <ChatPanel key={chatGeneration}
            messages={messages} busy={busy} explain={explain} restoring={restoring}
            capabilities={health?.capabilities} routingConfig={routingConfig} base={settings.apiBaseUrl}
            onSend={(text) => { setPinned(null); send(text, streaming) }}
            onInspect={(m) => { setPinned(m.id); setInspector(true) }}
          />
        </div>
        {route.view === 'knowledge' && (
          <KnowledgePanel base={settings.apiBaseUrl} mode={route.mode} docId={route.docId}
                          category={route.category} navigate={navigate} />
        )}
        {route.view === 'tickets' && <TicketsPanel base={settings.apiBaseUrl} ticketId={route.ticketId} />}
        {route.view === 'admin' && (
          <AdminPanel base={settings.apiBaseUrl} tab={route.tab} settings={settings} streaming={streaming}
                      onSave={saveConnection} inspector={inspector} theme={theme} onToggleTheme={toggle} />
        )}
      </main>

      <InspectorDrawer open={drawerOpen} onClose={() => setInspector(false)}>
        <div className="inspector-body scroll">{inspector}</div>
      </InspectorDrawer>
    </div>
  )
}
