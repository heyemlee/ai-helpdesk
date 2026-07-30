import { useRef, useState } from 'react'
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
  { id: 'knowledge', key: 'nav.knowledge', icon: 'book' },
  { id: 'monitor', key: 'nav.monitor', icon: 'pulse' },
  { id: 'eval', key: 'nav.eval', icon: 'checklist' },
]

export function Sidebar({ settings, onChange, view, onView, onNewChat, theme, onToggleTheme, busy }: {
  busy?: boolean
  settings: Settings; onChange: (patch: Partial<Settings>) => void
  view: View; onView: (v: View) => void; onNewChat: () => void
  theme: Theme; onToggleTheme: () => void
}) {
  const health = useHealth(settings.apiBaseUrl)
  const routing = health.data?.routing
  const { t, lang, toggle: toggleLang } = useT()
  const dialog = useRef<HTMLDialogElement>(null)
  const openedConversation = useRef(settings.conversationId)
  const [draft, setDraft] = useState(settings)
  const status = health.isError ? 'down' : health.data?.status === 'ok' ? 'ok' : 'connecting'
  return (
    <>
      <aside className="rail">
        <div className="rail-head">
          <div className="brand">
            <span className="brand-mark"><Icon name="chat" size={22} /></span>
            <span><span className="brand-name">ai-helpdesk<span className="brand-period">.</span></span><span className="brand-sub">{t('brand.sub')}</span></span>
          </div>
        </div>
        <div className="rail-body scroll">
          <button className="nav-item new-chat" disabled={busy} onClick={onNewChat} title={t('shell.newChat')}><Icon name="plus" /><span>{t('shell.newChat')}</span></button>
          <p className="rail-label">{t('nav.section.workbench')}</p>
          <nav aria-label={t('nav.section.workbench')}>
            {NAV.map((n) => (
              <button key={n.id} className={`nav-item${view === n.id ? ' is-active' : ''}`}
                aria-current={view === n.id ? 'page' : undefined} title={t(n.key)} onClick={() => onView(n.id)}>
                <Icon name={n.icon} /><span>{t(n.key)}</span>
              </button>
            ))}
          </nav>
        </div>
        <div className="rail-foot">
          <div className="service-status"><span>{t('shell.status')}</span><Chip tone={status === 'ok' ? 'success' : status === 'down' ? 'danger' : 'neutral'} dot>{t(`shell.status.${status}`)}</Chip></div>
          <button className="nav-item settings-trigger" title={t('shell.preferences')} onClick={() => { setDraft(settings); openedConversation.current = settings.conversationId; dialog.current?.showModal() }}>
            <Icon name="settings" /><span>{t('shell.preferences')}</span>
          </button>
          <div className="rail-utilities">
            <button className="btn btn-ghost" onClick={onToggleTheme} title={t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')} aria-label={t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} /></button>
            <button className="btn btn-ghost" onClick={toggleLang} data-od-id="lang-toggle" aria-label={t(lang === 'zh' ? 'shell.lang.toEn' : 'shell.lang.toZh')}>{lang === 'zh' ? 'EN' : '中文'}</button>
            <a className="btn btn-ghost" target="_blank" rel="noreferrer" href={`${settings.apiBaseUrl.replace(/\/+$/, '')}/docs`}>{t('shell.apiDocs')}</a>
          </div>
        </div>
      </aside>
      <dialog className="modal settings-modal" ref={dialog} aria-labelledby="settings-title">
        <form className="card" onSubmit={(e) => { e.preventDefault(); onChange({ ...draft, conversationId: draft.conversationId === openedConversation.current ? settings.conversationId : draft.conversationId }); dialog.current?.close() }}>
          <header className="card-head"><h2 id="settings-title">{t('shell.preferences')}</h2><span className="spacer" /><button type="button" className="icon-btn" aria-label={t('shell.close')} onClick={() => dialog.current?.close()}><Icon name="close" /></button></header>
          <div className="card-body settings-body">
            <div className="settings-preferences">
              <button type="button" className="btn btn-secondary" onClick={onToggleTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} />{t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}</button>
              <button type="button" className="btn btn-secondary" onClick={toggleLang}>{lang === 'zh' ? 'English' : '中文'}</button>
            </div>
            <div><h3>{t('shell.settings')}</h3><p className="page-lede">{t('shell.connectionHint')}</p></div>
            <Field label={t('shell.apiBase')} value={draft.apiBaseUrl} onChange={(apiBaseUrl) => setDraft({ ...draft, apiBaseUrl })} placeholder="/api" />
            <Field label={t('shell.userId')} value={draft.userId} onChange={(userId) => setDraft({ ...draft, userId })} placeholder="u1001" />
            <Field label={t('shell.convId')} value={draft.conversationId} onChange={(conversationId) => setDraft({ ...draft, conversationId })} placeholder={t('shell.convId.ph')} />
            {routing && <details className="raw"><summary>{t('shell.status')}</summary><dl><KV label={t('shell.specialists')}>{t('shell.specialists.v', { types: routing.specialists.length, replicas: routing.replicas_per_type })}</KV><KV label={t('shell.intents')}>{routing.intent.fine_grained_intents}</KV><KV label="Embedding">{routing.intent.embedding_backend}</KV></dl></details>}
            <div className="settings-actions"><button type="button" className="btn btn-secondary" onClick={() => dialog.current?.close()}>{t('kb.cancel')}</button><button className="btn btn-primary" type="submit">{t('shell.save')}</button></div>
          </div>
        </form>
      </dialog>
    </>
  )
}

function Field({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return <label className="field-row"><span>{label}</span><input className="field" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} /></label>
}
