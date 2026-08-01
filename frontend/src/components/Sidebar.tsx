import { useHealth } from '@/api/hooks'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { Chip } from './ui'
import { Icon, type IconName } from './Icon'
import { href, type View } from '@/lib/route'
import type { Theme } from '@/hooks/useTheme'

/** 日常工作的三件事。其余的都在「管理与诊断」里。 */
const PRIMARY: Array<{ id: View; key: Key; icon: IconName; to: string }> = [
  { id: 'chat', key: 'nav.chat', icon: 'chat', to: href.chat() },
  { id: 'knowledge', key: 'nav.knowledge', icon: 'book', to: href.knowledge() },
  { id: 'tickets', key: 'nav.tickets', icon: 'checklist', to: href.tickets() },
]

export function Sidebar({ base, view, onNewChat, theme, onToggleTheme, busy }: {
  base: string
  view: View
  onNewChat: () => void
  theme: Theme
  onToggleTheme: () => void
  busy?: boolean
}) {
  const health = useHealth(base)
  const { t, lang, toggle: toggleLang } = useT()
  const status = health.isError ? 'down' : health.data?.status === 'ok' ? 'ok' : 'connecting'
  // 待处理工单数来自 /health 的工单统计；后端没报就不显示数字
  const open = health.data?.tickets?.open
  return (
    <aside className="rail">
      <div className="rail-head">
        <div className="brand">
          <span className="brand-mark"><Icon name="chat" size={22} /></span>
          <span><span className="brand-name">ai-helpdesk</span><span className="brand-sub">{t('brand.sub')}</span></span>
        </div>
      </div>
      <div className="rail-body scroll">
        <button className="nav-item new-chat" disabled={busy} onClick={onNewChat} title={t('shell.newChat')}>
          <Icon name="plus" /><span>{t('shell.newChat')}</span>
        </button>
        <nav aria-label={t('nav.section.workbench')}>
          {PRIMARY.map((n) => (
            <a key={n.id} href={n.to} className={`nav-item${view === n.id ? ' is-active' : ''}`}
               aria-current={view === n.id ? 'page' : undefined} title={t(n.key)}>
              <Icon name={n.icon} /><span>{t(n.key)}</span>
              {n.id === 'tickets' && !!open && (
                <span className="count" title={t('nav.tickets.open', { n: open })}>
                  {open}<span className="sr-only"> {t('nav.tickets.openSr')}</span>
                </span>
              )}
            </a>
          ))}
        </nav>
      </div>
      <div className="rail-foot">
        <nav aria-label={t('nav.section.admin')}>
          <a href={href.admin()} className={`nav-item nav-admin${view === 'admin' ? ' is-active' : ''}`}
             aria-current={view === 'admin' ? 'page' : undefined} title={t('nav.admin')}>
            <Icon name="settings" />
            <span><span className="label-full">{t('nav.admin')}</span><span className="label-short">{t('nav.admin.short')}</span></span>
          </a>
        </nav>
        <div className="service-status">
          <span>{t('shell.status')}</span>
          <Chip tone={status === 'ok' ? 'success' : status === 'down' ? 'danger' : 'neutral'} dot>
            {t(`shell.status.${status}`)}
          </Chip>
        </div>
        <div className="rail-utilities">
          <button className="btn btn-ghost" onClick={onToggleTheme}
                  title={t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}
                  aria-label={t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}>
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
          </button>
          <button className="btn btn-ghost" onClick={toggleLang} data-od-id="lang-toggle"
                  aria-label={t(lang === 'zh' ? 'shell.lang.toEn' : 'shell.lang.toZh')}>
            {lang === 'zh' ? 'EN' : '中文'}
          </button>
        </div>
      </div>
    </aside>
  )
}
