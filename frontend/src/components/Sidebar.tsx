import { useHealth } from '@/api/hooks'
import { Chip, KV } from './ui'
import { Icon, type IconName } from './Icon'
import type { Settings } from '@/api/client'
import type { Theme } from '@/hooks/useTheme'

export type View = 'chat' | 'monitor' | 'eval' | 'knowledge'

const NAV: Array<{ id: View; label: string; icon: IconName }> = [
  { id: 'chat', label: '对话工作台', icon: 'chat' },
  { id: 'monitor', label: '在线监控', icon: 'pulse' },
  { id: 'eval', label: '端到端评测', icon: 'check' },
  { id: 'knowledge', label: '知识库', icon: 'book' },
]

export function Sidebar({ settings, onChange, view, onView, onNewChat, theme, onToggleTheme }: {
  settings: Settings
  onChange: (patch: Partial<Settings>) => void
  view: View
  onView: (v: View) => void
  onNewChat: () => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const health = useHealth(settings.apiBaseUrl)
  const routing = health.data?.routing

  return (
    <aside className="rail">
      <div className="rail-head">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><Icon name="spark" size={16} /></span>
          <span style={{ minWidth: 0 }}>
            <span className="brand-name">ai-helpdesk</span>
            <span className="brand-sub" style={{ display: 'block' }}>橱柜工厂客服编排</span>
          </span>
        </div>
      </div>

      <div className="rail-body scroll">
        <button className="nav-item" onClick={onNewChat}>
          <Icon name="plus" />新建对话
        </button>

        <p className="rail-label">工作台</p>
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`nav-item${view === n.id ? ' is-active' : ''}`}
            aria-current={view === n.id ? 'page' : undefined}
            onClick={() => onView(n.id)}
          >
            <Icon name={n.icon} />{n.label}
          </button>
        ))}
      </div>

      <div className="rail-foot">
        <div className="card" style={{ borderRadius: 'var(--radius-xl)' }}>
          <div className="card-body" style={{ padding: '10px 12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <span className="section-label" style={{ margin: 0 }}>服务状态</span>
              {health.isError
                ? <Chip tone="danger" dot>不可达</Chip>
                : health.data?.status === 'ok'
                  ? <Chip tone="success" dot>正常</Chip>
                  : <Chip dot>连接中</Chip>}
            </div>
            {routing && (
              <dl style={{ margin: '8px 0 0' }}>
                <KV label="专家 Agent">
                  {routing.specialists.length} 类 · 每类 {routing.replicas_per_type} 实例
                </KV>
                <KV label="细粒度意图">{routing.intent.fine_grained_intents}</KV>
                <KV label="Embedding">
                  <span className="mono">{routing.intent.embedding_backend}</span>
                </KV>
              </dl>
            )}
          </div>
        </div>

        <details className="raw" style={{ marginTop: 8 }}>
          <summary>连接设置</summary>
          <div style={{ display: 'grid', gap: 6, marginTop: 8 }}>
            <Field label="API 地址" value={settings.apiBaseUrl}
                   onChange={(v) => onChange({ apiBaseUrl: v })} placeholder="/api" />
            <Field label="用户 ID" value={settings.userId}
                   onChange={(v) => onChange({ userId: v })} placeholder="u1001" />
            <Field label="会话 ID" value={settings.conversationId}
                   onChange={(v) => onChange({ conversationId: v })} placeholder="自动生成" />
          </div>
        </details>

        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <button
            className="btn btn-ghost" style={{ flex: 1 }} onClick={onToggleTheme}
            aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          >
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
          </button>
          <a
            className="btn btn-ghost" style={{ flex: 1 }} target="_blank" rel="noreferrer"
            href={`${settings.apiBaseUrl.replace(/\/+$/, '')}/docs`}
          >
            API 文档
          </a>
        </div>
      </div>
    </aside>
  )
}

function Field({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string
}) {
  return (
    <label className="field-row">
      <span>{label}</span>
      <input className="field" value={value} placeholder={placeholder}
             onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}
