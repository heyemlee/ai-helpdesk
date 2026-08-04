import { useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useHealth, useSops } from '@/api/hooks'
import { getStaffKey, type Settings } from '@/api/client'
import { Card, Chip, KV, StateBlock } from './ui'
import { EvalPanel } from './EvalPanel'
import { Icon } from './Icon'
import { ModelSettings } from './ModelSettings'
import { MonitorPanel } from './MonitorPanel'
import { agentDisplayName, agentLabel, dateTime } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { llmReasonKey, llmRecovery } from '@/lib/llm'
import { ADMIN_TABS, href, type AdminTab } from '@/lib/route'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type { Theme } from '@/hooks/useTheme'

const TAB_LABEL: Record<AdminTab, Key> = {
  overview: 'ad.tab.overview',
  models: 'ad.tab.models',
  sops: 'ad.tab.sops',
  monitor: 'ad.tab.monitor',
  eval: 'ad.tab.eval',
  inspector: 'ad.tab.inspector',
}

export interface ConnectionDraft extends Settings {
  staffKey: string
  streaming: boolean
}

/**
 * 管理与诊断：主管和开发用的东西都收在这里——连接配置、模型信息、SOP、
 * 监控、评测、编排检查器。普通员工的日常（对话 / 知识库 / 工单）用不到这一页，
 * 所以它不在主导航的三项里。
 *
 * 监控和评测是原来的面板原样搬进来的；编排检查器由上层把最近一轮的内容传进来。
 */
export function AdminPanel({ base, tab, settings, streaming, onSave, inspector, theme, onToggleTheme }: {
  base: string
  tab: AdminTab
  settings: Settings
  streaming: boolean
  onSave: (draft: ConnectionDraft) => void
  inspector: ReactNode
  theme: Theme
  onToggleTheme: () => void
}) {
  const { t, lang, toggle: toggleLang } = useT()
  return (
    <div className="admin">
      <div className="admin-head">
        <p className="admin-lede">{t('ad.lede')}</p>
        <div className="tabs" role="tablist" aria-label={t('nav.admin')}>
          {ADMIN_TABS.map((id) => (
            <a key={id} role="tab" aria-selected={tab === id} href={href.admin(id)}
               className={`tab${tab === id ? ' is-active' : ''}`}>
              {t(TAB_LABEL[id])}
            </a>
          ))}
        </div>
      </div>

      {tab === 'monitor' && <MonitorPanel base={base} />}
      {tab === 'models' && <ModelSettings base={base} />}
      {tab === 'eval' && <EvalPanel base={base} />}
      {tab === 'overview' && (
        <div className="page scroll"><div className="page-inner">
          <Overview base={base} />
          <Connection settings={settings} streaming={streaming} onSave={onSave} />
          {/* 窄屏的底部导航放不下主题和语言开关，这里是它们在手机上的唯一入口 */}
          <Card title={t('ad.display')}>
            <div className="display-prefs">
              <button type="button" className="btn btn-secondary" onClick={onToggleTheme}>
                <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
                {t(theme === 'dark' ? 'shell.theme.toLight' : 'shell.theme.toDark')}
              </button>
              <button type="button" className="btn btn-secondary" onClick={toggleLang}>
                {t(lang === 'zh' ? 'shell.lang.toEn' : 'shell.lang.toZh')}
              </button>
            </div>
          </Card>
        </div></div>
      )}
      {tab === 'sops' && (
        <div className="page scroll"><div className="page-inner"><Sops base={base} /></div></div>
      )}
      {tab === 'inspector' && (
        <div className="page scroll"><div className="page-inner">
          <div>
            <h2 className="page-title">{t('ad.tab.inspector')}</h2>
            <p className="page-lede">{t('ad.inspector.lede')}</p>
          </div>
          <Card><div className="inspector-inline">{inspector}</div></Card>
        </div></div>
      )}
    </div>
  )
}

/** 服务状态和模型信息：全部来自 /health，后端没报的项目写"未报告"。 */
function Overview({ base }: { base: string }) {
  const { t } = useT()
  const health = useHealth(base)
  const data = health.data

  if (health.isError) {
    const { body, detail } = explainError(health.error, t)
    return <StateBlock tone="danger" title={t('shell.status.down')} detail={detail}>{body}</StateBlock>
  }
  if (!data) return <StateBlock busy title={t('mon.loading')} />

  const yesNo = (value: boolean | undefined, yes: Key, no: Key) =>
    value === undefined ? t('ad.notReported') : t(value ? yes : no)
  const agents = Object.entries(data.agents ?? {})
  const models = [...new Set(agents.map(([, a]) => a.model))]
  // 服务在线和模型可用是两件事：后端活着、/health 答得上来，模型凭证照样可能被停用
  const llmDown = data.llm?.available === false
  const retryIn = data.llm?.retry_after_s

  return (
    <div className="admin-grid">
      <Card title={t('ad.service')} badge={<Chip tone="success" dot>{t('shell.status.ok')}</Chip>}>
        <dl>
          <KV label={t('ad.businessData')}>
            {yesNo(data.capabilities?.business_data, 'ad.dataConfigured', 'ad.notConfigured')}
          </KV>
          <KV label={t('ad.notifications')}>
            {yesNo(data.capabilities?.ticket_notifications, 'ad.configured', 'ad.notConfigured')}
          </KV>
          <KV label={t('ad.storage')}>{data.storage_backend ?? t('ad.notReported')}</KV>
          <KV label={t('ad.retrieval')}>{data.retrieval?.mode ?? t('ad.notReported')}</KV>
          <KV label={t('ad.memory')}>{yesNo(data.memory?.cross_session, 'ad.on', 'ad.off')}</KV>
          <KV label={t('ad.userToken')}>{yesNo(data.auth?.user_token, 'ad.on', 'ad.off')}</KV>
        </dl>
      </Card>

      <Card title={t('ad.models')}
            badge={llmDown ? <Chip tone="danger" dot>{t('ad.llm.down')}</Chip> : undefined}>
        {llmDown && (
          <p className="note danger" role="alert" style={{ marginBottom: 12 }}>
            {t('ad.llm.cause', { reason: t(llmReasonKey(data.llm?.unavailable_reason)) })}{' '}
            {llmRecovery(data.llm?.unavailable_reason) === 'reset' || typeof retryIn !== 'number'
              ? t('ad.llm.reset')
              : t('ad.llm.retry', { n: Math.max(1, Math.ceil(retryIn)) })}
            {(data.llm?.blocked_calls ?? 0) > 0 && <> {t('ad.llm.blocked', { n: data.llm!.blocked_calls! })}</>}
          </p>
        )}
        <dl>
          <KV label={t('ad.llmStatus')}>
            {data.llm?.available === undefined ? t('ad.notReported')
              : t(llmDown ? 'ad.llm.down' : 'ad.llm.available')}
          </KV>
          <KV label={t('ad.llm')}>
            <span className="mono">{data.llm?.model ?? (models.join(', ') || t('ad.notReported'))}</span>
          </KV>
          <KV label={t('ad.provider')}>{data.llm?.provider ?? t('ad.notReported')}</KV>
          <KV label={t('ad.rerank')}>{data.retrieval?.rerank ?? t('ad.notReported')}</KV>
          <KV label={t('ad.embedding')}>
            {data.routing?.intent?.embedding_backend ?? t('ad.notReported')}
          </KV>
          {agents.map(([key, agent]) => (
            <KV key={key} label={agentDisplayName(key.replace(/_\d+$/, ''), t, agent.display_name)}>
              <span className="mono">{agent.model}</span>
            </KV>
          ))}
        </dl>
      </Card>
    </div>
  )
}

/**
 * 连接配置。默认值就能用——这张表单只在把工作台指向另一套后端时才需要动。
 * 改动在点保存后才生效，输入到一半不会把正在进行的对话带到别处去。
 */
function Connection({ settings, streaming, onSave }: {
  settings: Settings; streaming: boolean; onSave: (draft: ConnectionDraft) => void
}) {
  const { t } = useT()
  const [draft, setDraft] = useState<ConnectionDraft>(
    () => ({ ...settings, staffKey: getStaffKey(), streaming }),
  )
  const [saved, setSaved] = useState(false)
  const set = (patch: Partial<ConnectionDraft>) => { setSaved(false); setDraft((d) => ({ ...d, ...patch })) }

  return (
    <Card title={t('ad.connection')}
          actions={<a className="btn btn-ghost" target="_blank" rel="noreferrer"
                      href={`${settings.apiBaseUrl.replace(/\/+$/, '')}/docs`}>{t('shell.apiDocs')}</a>}>
      <form className="settings-body" onSubmit={(e) => { e.preventDefault(); onSave(draft); setSaved(true) }}>
        <p className="page-lede">{t('ad.connection.hint')}</p>
        <Field label={t('shell.apiBase')} value={draft.apiBaseUrl} placeholder="/api"
               onChange={(apiBaseUrl) => set({ apiBaseUrl })} />
        <Field label={t('ad.staffKey')} hint={t('ad.staffKey.hint')} value={draft.staffKey} type="password"
               autoComplete="off" onChange={(staffKey) => set({ staffKey })} />
        <Field label={t('shell.userId')} hint={t('ad.userId.hint')} value={draft.userId} placeholder="u1001"
               onChange={(userId) => set({ userId })} />
        <Field label={t('shell.convId')} hint={t('ad.convId.hint')} value={draft.conversationId}
               placeholder={t('shell.convId.ph')} onChange={(conversationId) => set({ conversationId })} />
        <label className="switch">
          <input type="checkbox" checked={draft.streaming} onChange={(e) => set({ streaming: e.target.checked })} />
          <span className="track" />{t('ad.stream')}
        </label>
        <div className="settings-actions">
          {saved && <span className="saved-note" role="status">{t('ad.saved')}</span>}
          <button className="btn btn-primary" type="submit">{t('shell.save')}</button>
        </div>
      </form>
    </Card>
  )
}

function Field({ label, hint, value, onChange, placeholder, type = 'text', autoComplete }: {
  label: string; hint?: string; value: string; onChange: (v: string) => void
  placeholder?: string; type?: string; autoComplete?: string
}) {
  return (
    <label className="field-row">
      <span>{label}</span>
      <input className="field" type={type} value={value} placeholder={placeholder}
             autoComplete={autoComplete} onChange={(e) => onChange(e.target.value)} />
      {hint && <small className="field-hint">{hint}</small>}
    </label>
  )
}

/**
 * SOP 只读入口。SOP 规定处理步骤和升级条件，知识文档说明业务事实和规则——
 * 两样东西分开放，员工不会在知识检索结果里把流程当成事实读。
 */
function Sops({ base }: { base: string }) {
  const { t, lang } = useT()
  const sops = useSops(base)

  const heading = (
    <div>
      <h2 className="page-title">{t('sop.pageTitle')}</h2>
      <p className="page-lede">{t('sop.lede')}</p>
    </div>
  )

  if (sops.isError) {
    const { body, detail } = explainError(sops.error, t, 'sop.error.404')
    return <>{heading}<StateBlock tone="danger" title={t('sop.error.title')} detail={detail}
      action={<button className="btn btn-secondary" onClick={() => sops.refetch()}>{t('kb.retry')}</button>}>
      {body}</StateBlock></>
  }
  if (!sops.data) return <>{heading}<StateBlock busy title={t('sop.loading')} /></>
  if (sops.data.sops.length === 0) return <>{heading}<StateBlock title={t('sop.empty')} /></>

  return (
    <>
      {heading}
      {sops.data.errors > 0 && <p className="note caution">{t('sop.errors', { n: sops.data.errors })}</p>}
      <div className="sop-list">
        {sops.data.sops.map((sop) => {
          const updated = dateTime(sop.modified_at, lang)
          return (
            <details className="sop" key={sop.source}>
              <summary>
                <span className="sop-name">{sop.name}</span>
                {!sop.enabled && <Chip tone="caution">{t('sop.disabled')}</Chip>}
                {sop.description && <span className="sop-desc">{sop.description}</span>}
              </summary>
              <dl className="reader-meta">
                <div>
                  <dt>{t('sop.appliesTo')}</dt>
                  <dd>{sop.agents.length ? sop.agents.map((a) => agentLabel(a, t)).join(', ') : t('sop.allAgents')}</dd>
                </div>
                <div>
                  <dt>{t('sop.triggers')}</dt>
                  <dd>{sop.keywords.length ? sop.keywords.join(', ') : t('sop.always')}</dd>
                </div>
                <div>
                  <dt>{t('sop.source')}</dt>
                  <dd><span className="mono">{sop.source}</span>{updated && <> · {t('sop.updated', { date: updated })}</>}</dd>
                </div>
              </dl>
              <div className="prose-chat reader-body"><Markdown remarkPlugins={[remarkGfm]}>{sop.content}</Markdown></div>
            </details>
          )
        })}
      </div>
    </>
  )
}
