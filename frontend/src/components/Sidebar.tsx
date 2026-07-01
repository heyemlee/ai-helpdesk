import { useHealth } from '@/api/hooks'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { Chip, KV } from './ui'
import { Icon, type IconName } from './Icon'
import type { Settings } from '@/api/client'
import type { Theme } from '@/hooks/useTheme'

export type View = 'chat' | 'monitor' | 'eval' | 'knowledge'

const NAV: Array<{ id: View; key: Key; icon: IconName }> = [
  { id: 'chat', key: 'nav.chat', icon: 'chat' },
  { id: 'monitor', key: 'nav.monitor', icon: 'pulse' },
  { id: 'eval', key: 'nav.eval', icon: 'check' },
  { id: 'knowledge', key: 'nav.knowledge', icon: 'book' },
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
  const { t, lang, toggle: toggleLang } = useT()

  return (
    <aside className="rail">
      <div className="rail-head">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><Icon name="spark" size={16} /></span>
          <span style={{ minWidth: 0 }}>
            <span className="brand-name">ai-helpdesk</span>
            <span className="brand-sub" style={{ display: 'block' }}>{t('brand.sub')}</span>
          </span>
        </div>
      </div>

      <div className="rail-body scroll">
        <button className="nav-item" onClick={onNewChat}>
          <Icon name="plus" />{t('shell.newChat')}
        </button>

        <p className="rail-label">{t('nav.section.workbench')}</p>
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`nav-item${view === n.id ? ' is-active' : ''}`}
            aria-current={view === n.id ? 'page' : undefined}
            onClick={() => onView(n.id)}
          >
            <Icon name={n.icon} />{t(n.key)}
          </button>
        ))}
      </div>

      <div className="rail-foot">
        <div className="card" style={{ borderRadius: 'var(--radius-xl)' }}>
          <div className="card-body" style={{ padding: '10px 12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <span className="section-label" style={{ margin: 0 }}>{t('shell.status')}</span>
              {health.isError
                ? <Chip tone="danger" dot>{t('shell.status.down')}</Chip>
                : health.data?.status === 'ok'
                  ? <Chip tone="success" dot>{t('shell.status.ok')}</Chip>
                  : <Chip dot>{t('shell.status.connecting')}</Chip>}
            </div>
            {routing && (
              <dl style={{ margin: '8px 0 0' }}>
                <KV label={t('shell.specialists')}>
                  {t('shell.specialists.v', { types: routing.specialists.length, replicas: routing.replicas_per_type })}
                </KV>
                <KV label={t('shell.intents')}>{routing.intent.fine_grained_intents}</KV>
                <KV label="Embedding">
                  <span className="mono">{routing.intent.embedding_backend}</span>
                </KV>
              </dl>
            )}
          </div>
        </div>

        <details className="raw" style={{ marginTop: 8 }}>
          <summary>{t('shell.settings')}</summary>
          <div style={{ display: 'grid', gap: 6, marginTop: 8 }}>
            <Field label={t('shell.apiBase')} value={settings.apiBaseUrl}
                   onChange={(v) => onChange({ apiBaseUrl: v })} placeholder="/api" />
            <Field label={t('shell.userId')} value={settings.userId}
                   onChange={(v) => onChange({ userId: v })} placeholder="u1001" />
            <Field label={t('shell.convId')} value={settings.conversationId}
                   onChange={(v) => onChange({ conversationId: v })} placeholder={t('shell.convId.ph')} />
          </div>
        </details>

        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <button
            className="btn btn-ghost" style={{ flex: 1 }} onClick={onToggleTheme}
            aria-label={t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}
          >
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
          </button>
          {/* 按钮上写的是**切过去之后**的语言，和主题按钮显示目标状态图标同一套逻辑 */}
          <button
            className="btn btn-ghost" style={{ flex: 1 }} onClick={toggleLang}
            data-od-id="lang-toggle"
            aria-label={t(lang === 'zh' ? 'shell.lang.toEn' : 'shell.lang.toZh')}
          >
            {lang === 'zh' ? 'EN' : '中文'}
          </button>
          <a
            className="btn btn-ghost" style={{ flex: 1 }} target="_blank" rel="noreferrer"
            href={`${settings.apiBaseUrl.replace(/\/+$/, '')}/docs`}
          >
            {t('shell.apiDocs')}
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
