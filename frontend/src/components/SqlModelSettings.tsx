import { useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { SqlApiError, sqlModelApi } from '@/api/sql/client'
import type { SqlModelList, SqlModelStatus, SqlProbeReport } from '@/api/sql/types'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { explainSqlError } from '@/lib/sql/errors'
import { hasKey } from '@/lib/sql/labels'
import {
  canKeepEmbeddingKey, canKeepKey, embeddingPrefill, embeddingRequestBody, insecurePage, knownRejection,
  modelChoices, originOf, prefill, requestBody, SQL_PROVIDERS, type SqlEmbeddingForm, type SqlModelForm,
  type SqlProvider,
} from '@/lib/sql/modelForm'
import { Card, Chip, StateBlock } from './ui'
import { ConfirmBar, InUse } from './ModelSettings'
import { Icon } from './Icon'

/**
 * 模型连接页的 SQL Agent 那一栏（移植自旧 SQL 控制台的 settingsModel.js / settingsEmbedding.js）。
 *
 * 版式和左边客服那一栏一样：最上面写清楚现在用的是哪个模型，一个下拉框换模型、一个保存键；
 * 协议、接口地址、Key、SQL 单独用的模型收在「连接设置」里。保存前先测一次连接，测不通就不存
 * （可以选「仍然保存」）。确认都在页面里做，不用 window.confirm。
 *
 * Key 只往 SQL Agent 送：输入框不预填、只在这个组件的内存里、保存成功就清空，不进 localStorage /
 * sessionStorage；状态里只有"配没配"和末四位。浏览器到 SQL Agent 走控制台的 /sql-api/，
 * 那一段的管理员 Key 由 nginx 在服务端加。
 */
const STATUS_KEY = ['sql', 'model-status'] as const

/** 下拉框里「手动输入」那一项的值。模型名不能以空格开头，撞不上真实的名字。 */
const CUSTOM = ' custom'

type Busy = '' | 'test' | 'save' | 'restore'
type T = ReturnType<typeof useT>['t']

const hostOf = (address: string | undefined) => {
  try { return new URL(address ?? '').host } catch { return address ?? '' }
}

const providerLabel = (t: T, provider: string) =>
  (hasKey(`sq.model.provider.${provider}`) ? t(`sq.model.provider.${provider}` as Key) : provider)

const outcomeLabel = (t: T, outcome: string) =>
  (hasKey(`sq.model.outcome.${outcome}`) ? t(`sq.model.outcome.${outcome}` as Key) : outcome)

const roleLabel = (t: T, role: string) =>
  (hasKey(`sq.model.role.${role}`) ? t(`sq.model.role.${role}` as Key) : role)

/** 服务端给了原话就用原话（哪个字段不对、为什么不让改，它说得比状态码清楚） */
function failureText(error: unknown, t: T): string {
  if (error instanceof SqlApiError && error.serverMessage) return error.serverMessage
  return explainSqlError(error, t).body
}

function savedText(t: T, source: string, savedAt?: string): string {
  if (source !== 'page') return t('sq.model.source.server')
  const when = (savedAt ?? '').replace('T', ' ').slice(0, 16)
  return when ? t('sq.model.source.pageAt', { when }) : t('sq.model.source.page')
}

function keyText(t: T, configured: boolean, hint?: string): string {
  if (!configured) return t('sq.model.state.noKey')
  return hint ? t('sq.model.key.ends', { hint }) : t('sq.model.key.set')
}

export function SqlModelSettings() {
  const { t } = useT()
  const status = useQuery({ queryKey: STATUS_KEY, queryFn: () => sqlModelApi.status(), retry: false })

  if (status.isError) {
    // 老版本的 SQL Agent 没有这组接口：不是出错，是没有这个功能
    if (status.error instanceof SqlApiError && status.error.status === 404) {
      return <p className="note neutral">{t('sq.model.unsupported')}</p>
    }
    const { body, detail } = explainSqlError(status.error, t)
    return <StateBlock tone="danger" title={t('sq.model.loadFailed')} detail={detail || undefined}>{body}</StateBlock>
  }
  if (!status.data) return <StateBlock busy title={t('sq.model.loading')} />
  const data = status.data
  return (
    <>
      {!data.editable && (
        <div className="note neutral" role="note">
          <strong>{hasKey(`sq.model.reason.${data.reason}`) ? t(`sq.model.reason.${data.reason}` as Key) : data.reason}</strong>
          {hasKey(`sq.model.reason.${data.reason}.body`) && <p>{t(`sq.model.reason.${data.reason}.body` as Key)}</p>}
        </div>
      )}
      <ChatCard status={data} />
      <EmbeddingCard status={data} />
    </>
  )
}

// ── 下拉框 ────────────────────────────────────────────────────────────────────

/** 下拉框的候选从哪个连接读。Key 只在组件状态里，不进查询缓存的键。 */
interface ListTarget { provider: SqlProvider; baseUrl: string; key: string; stamp: number }
let listStamp = 0

function useModelList(section: 'chat' | 'embedding', target: ListTarget, enabled: boolean) {
  return useQuery<SqlModelList>({
    queryKey: ['sql', 'model-list', section, target.provider, target.baseUrl, target.stamp],
    queryFn: () => (section === 'chat'
      ? sqlModelApi.listModels({ provider: target.provider, base_url: target.baseUrl, api_key: target.key,
                                 chat_model: '', coder_model: '' })
      : sqlModelApi.listEmbeddingModels({ base_url: target.baseUrl, api_key: target.key })),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  })
}

function listHint(t: T, list: ReturnType<typeof useModelList>, needKey: boolean): string {
  if (needKey) return t('sq.model.list.needKey')
  if (list.isFetching) return t('sq.model.list.loading')
  if (list.isError) return t('sq.model.list.failed', { reason: failureText(list.error, t) })
  if (list.data && !list.data.ok) return t('sq.model.list.failed', { reason: outcomeLabel(t, list.data.outcome) })
  if (list.data) return t('sq.model.list.count', { n: list.data.models.length })
  return ''
}

/** 选模型的控件：下拉框，或者选了「其他」之后的输入框。same 给了就是"和上面一样"那一项 */
function Picker({ id, value, options, typing, disabled, onPick, onType, same, sameSelected }: {
  id: string
  value: string
  options: string[]
  typing: boolean
  disabled: boolean
  onPick: (value: string) => void
  onType: (value: string) => void
  same?: string
  sameSelected?: boolean
}) {
  const { t } = useT()
  if (typing) {
    return (
      <input id={id} className="field" value={value} disabled={disabled} spellCheck={false} autoComplete="off"
             placeholder={t('sq.model.typeName')} onChange={(e) => onType(e.target.value)} />
    )
  }
  return (
    <select id={id} className="field" disabled={disabled} value={same && sameSelected ? '' : value}
            onChange={(e) => onPick(e.target.value)}>
      {same && <option value="">{same}</option>}
      {!same && !value && <option value="" disabled>{t('sq.model.typeName')}</option>}
      {modelChoices(options, value).map((name) => <option key={name} value={name}>{name}</option>)}
      <option value={CUSTOM}>{t('sq.model.custom')}</option>
    </select>
  )
}

// ── 测试结果 ──────────────────────────────────────────────────────────────────

function ProbeReport({ report, embedding }: { report: SqlProbeReport | null; embedding?: boolean }) {
  const { t } = useT()
  if (!report) return null
  if (report.ok) {
    const took = report.results.map((item) => (embedding
      ? `${t('sq.model.emb.dimensionsN', { dimensions: item.dimensions ?? 0 })} · ${item.duration_ms} ms`
      : `${item.roles.map((role) => roleLabel(t, role)).join(' / ')} ${item.duration_ms} ms`)).join(' · ')
    return <p className="note success" role="status"><Icon name="check" size={14} />{t('sq.model.report.ok')} · {took}</p>
  }
  return (
    <div className="note danger" role="status">
      <p>{t('sq.model.report.failed')}</p>
      <ul className="model-checks">
        {report.results.map((item, index) => (
          <li key={index}>
            <span className="mono">{item.model}</span>
            {t('sq.model.report.line', {
              roles: item.roles.map((role) => roleLabel(t, role)).join(' / '),
              outcome: outcomeLabel(t, item.outcome),
            })}
            {item.status_code && !item.ok ? ` · HTTP ${item.status_code}` : ''} · {item.duration_ms} ms
            {item.message && <p className="mono">{t('sq.model.report.server', { message: item.message })}</p>}
          </li>
        ))}
        {report.warnings.map((code) => (
          <li key={code}>{hasKey(`sq.model.note.${code}`) ? t(`sq.model.note.${code}` as Key) : code}</li>
        ))}
      </ul>
    </div>
  )
}

/** 一张卡片里的测试 / 保存 / 恢复。共用一个"在忙"状态，同一时间只做一件事 */
function useActions() {
  const { t } = useT()
  const [busy, setBusy] = useState<Busy>('')
  const [report, setReport] = useState<SqlProbeReport | null>(null)
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null)
  const [asking, setAsking] = useState<'' | 'save' | 'restore'>('')
  const [failedSave, setFailedSave] = useState(false)

  async function run(kind: Busy, task: () => Promise<void>) {
    setBusy(kind); setOutcome(null); setReport(null); setAsking(''); setFailedSave(false)
    try {
      await task()
    } catch (error) {
      // 保存前的测试没通过：服务端把测试结果一起带回来了（Embedding），照样列出来
      const detail = error instanceof SqlApiError ? error.detail as { test?: SqlProbeReport } | null : null
      if (detail?.test) setReport(detail.test)
      setOutcome({ ok: false, text: failureText(error, t) })
    } finally {
      setBusy('')
    }
  }
  /** 表单一改，上一次的结果和确认就作废 */
  const touch = () => { setReport(null); setOutcome(null); setAsking(''); setFailedSave(false) }
  return { busy, report, setReport, outcome, setOutcome, asking, setAsking, failedSave, setFailedSave, run, touch }
}

function Warnings({ items }: { items: Array<[Key, ReactNode?]> }) {
  const { t } = useT()
  return (
    <>
      {items.map(([title, body]) => (
        <div key={title} className="note caution" role="note"><strong>{t(title)}</strong>{body && <p>{body}</p>}</div>
      ))}
    </>
  )
}

function Outcome({ a, busyKey }: { a: ReturnType<typeof useActions>; busyKey: (busy: Exclude<Busy, ''>) => Key }) {
  const { t } = useT()
  return (
    <div role="status">
      {a.busy && <p className="field-hint"><span className="pulse" /> {t(busyKey(a.busy))}</p>}
      {a.outcome?.ok && <p className="note success"><Icon name="check" size={14} />{a.outcome.text}</p>}
      {a.outcome && !a.outcome.ok && <p className="note danger" role="alert">{t('sq.model.failed')} · {a.outcome.text}</p>}
    </div>
  )
}

// ── 对话模型 ──────────────────────────────────────────────────────────────────

function ChatCard({ status }: { status: SqlModelStatus }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<SqlModelForm>(() => prefill(status, status.provider))
  const [typedKey, setTypedKey] = useState('')
  const [coderSame, setCoderSame] = useState(() => status.coder_model === status.chat_model)
  const [typing, setTyping] = useState({ chat: false, coder: false })
  const [open, setOpen] = useState(() => !status.key_configured)
  const [target, setTarget] = useState<ListTarget>(() => ({
    provider: status.provider, baseUrl: (status.providers?.[status.provider]?.base_url ?? '').trim(), key: '', stamp: 0,
  }))
  const a = useActions()

  const coderModel = coderSame ? form.chatModel : form.coderModel
  const effective: SqlModelForm = { ...form, coderModel }
  const keepKey = canKeepKey(status, form)
  const hasUsableKey = typedKey.trim() !== '' || keepKey
  const current = status.providers?.[status.provider]
  const trimSlash = (value: string | undefined) => String(value ?? '').trim().replace(/\/+$/, '')
  const changed = form.provider !== status.provider
    || trimSlash(form.baseUrl) !== trimSlash(current?.base_url)
    || form.chatModel.trim() !== status.chat_model
    || coderModel.trim() !== status.coder_model
    || typedKey.trim() !== ''
  const savable = changed && hasUsableKey
  const listEnabled = status.editable && (!!target.key || canKeepKey(status, { provider: target.provider, baseUrl: target.baseUrl }))
  const list = useModelList('chat', target, listEnabled)
  const listed = list.data?.ok ? list.data.models : []
  // 读不到清单时退回表单里选的那个协议上配过的模型名——不是现在在用的那个协议的：
  // 从 Anthropic 换到 OpenAI 时，下拉框里冒出 claude-* 的话，选了存下去就是一份不通的连接
  const selected = status.providers?.[form.provider]
  const options = listed.length ? listed : [selected?.chat_model, selected?.coder_model].filter(Boolean) as string[]

  const settle = (next: SqlModelForm, key: string, force = false) => setTarget((prev) => (
    !force && prev.provider === next.provider && prev.baseUrl === next.baseUrl.trim() && prev.key === key.trim()
      ? prev
      : { provider: next.provider, baseUrl: next.baseUrl.trim(), key: key.trim(), stamp: ++listStamp }
  ))
  const set = (patch: Partial<SqlModelForm>) => { a.touch(); setForm((prev) => ({ ...prev, ...patch })) }

  const changeProvider = (provider: SqlProvider) => {
    const next = prefill(status, provider)
    a.touch()
    setForm(next)
    setTypedKey('')
    setCoderSame(next.coderModel === next.chatModel)
    setTyping({ chat: false, coder: false })
    settle(next, '', true)
  }

  /** 换上服务端回来的新状态：表单照它重填，Key 输入框清空（结果那句话留着） */
  const adopt = (next: SqlModelStatus) => {
    queryClient.setQueryData(STATUS_KEY, next)
    const fresh = prefill(next, next.provider)
    setForm(fresh)
    setTypedKey('')
    setCoderSame(fresh.coderModel === fresh.chatModel)
    setTyping({ chat: false, coder: false })
    settle(fresh, '', true)
  }

  /** 换协议、换主机才要确认：同一台主机上换个模型，问题本来就发给它 */
  const needsConfirm = () => {
    const body = requestBody(effective, typedKey)
    const official = status.providers?.[body.provider]?.official_base_url
    return body.provider !== status.provider || originOf(body.base_url || official) !== originOf(status.endpoint)
  }

  const save = (force = false) => a.run('save', async () => {
    const body = requestBody(effective, typedKey)
    if (!force) {
      const report = await sqlModelApi.test(body)
      if (!report.ok) {
        a.setReport(report)
        a.setFailedSave(true)
        return
      }
    }
    const saved = await sqlModelApi.save(body)
    a.setOutcome({ ok: true, text: t('sq.model.saved', { model: saved.status.chat_model }) })
    adopt(saved.status)
  })
  const submit = () => {
    if (!savable || a.busy) return
    if (needsConfirm()) a.setAsking('save')
    else void save()
  }
  const restore = () => a.run('restore', async () => {
    const cleared = await sqlModelApi.clear()
    a.setOutcome({ ok: true, text: t('sq.model.restored') })
    adopt(cleared.status)
  })
  const test = () => a.run('test', async () => a.setReport(await sqlModelApi.test(requestBody(effective, typedKey))))

  const rejection = status.editable ? knownRejection(status) : null
  let chip: ReactNode = null
  if (!status.key_configured) chip = <Chip tone="caution" dot>{t('sq.model.state.noKey')}</Chip>
  else if (rejection) chip = <Chip tone="danger" dot>{t('sq.model.key.rejected')}{rejection.status_code ? ` · HTTP ${rejection.status_code}` : ''}</Chip>
  else if (status.editable) chip = <Chip tone="success" dot>{t('sq.model.state.ok')}</Chip>

  const warnings: Array<[Key, ReactNode?]> = []
  if (insecurePage(window.location)) warnings.push(['sq.model.warn.insecurePage', t('sq.model.warn.insecurePage.body')])
  if (status.store_problem) warnings.push(['sq.model.warn.storeProblem', t('sq.model.warn.storeProblem.body')])
  if (status.custom_endpoint) warnings.push(['sq.model.warn.customEndpoint', t('sq.model.warn.customEndpoint.body')])

  const off = !!a.busy
  const entry = status.providers?.[form.provider]
  const server = status.server
  return (
    <Card title={t('sq.model.title')}>
      <InUse name={<span>{status.chat_model}</span>} state={chip} meta={[
        providerLabel(t, status.provider),
        status.coder_model !== status.chat_model && t('sq.model.coderIs', { model: status.coder_model }),
        status.endpoint && <span key="host" className="mono">{hostOf(status.endpoint)}</span>,
        status.key_configured && keyText(t, true, status.key_hint),
        status.editable && savedText(t, status.source, status.saved_at),
      ]} />
      {!status.key_configured && <p className="field-hint">{t('sq.model.key.missing')}</p>}

      {status.editable && (
        <form className="model-form" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <div className="model-pick">
            <label className="model-pick-label" htmlFor="sq-chat-model">{t('sq.model.model')}</label>
            <div className="model-pick-row">
              <Picker id="sq-chat-model" value={form.chatModel} options={options} typing={typing.chat} disabled={off}
                      onPick={(value) => (value === CUSTOM ? setTyping((x) => ({ ...x, chat: true })) : set({ chatModel: value }))}
                      onType={(value) => set({ chatModel: value })} />
              <button type="button" className="icon-btn" title={t('sq.model.list.refresh')} aria-label={t('sq.model.list.refresh')}
                      disabled={off || list.isFetching || !hasUsableKey} onClick={() => settle(form, typedKey, true)}>
                <Icon name="refresh" />
              </button>
              <button type="submit" className="btn btn-primary" disabled={off || !savable} aria-busy={a.busy === 'save'}>
                {t('sq.model.save')}
              </button>
            </div>
            <p className="model-pick-source">
              {listHint(t, list, !hasUsableKey)}
              {typing.chat && (
                <button type="button" className="link-btn" onClick={() => setTyping((x) => ({ ...x, chat: false }))}>
                  {t('sq.model.fromList')}
                </button>
              )}
            </p>
          </div>

          {a.asking === 'save' && (
            <ConfirmBar confirm={t('sq.model.confirm.yes')} onConfirm={() => void save()} onCancel={() => a.setAsking('')}>
              {t('sq.model.confirm.save', {
                address: originOf(requestBody(effective, typedKey).base_url || entry?.official_base_url) || form.baseUrl,
              })}
            </ConfirmBar>
          )}
          {a.asking === 'restore' && (
            <ConfirmBar confirm={t('sq.model.confirm.restoreYes')} onConfirm={() => void restore()} onCancel={() => a.setAsking('')}>
              {t('sq.model.confirm.restore', {
                target: server ? `${providerLabel(t, server.provider)} · ${server.chat_model}` : '—',
              })}
            </ConfirmBar>
          )}
          <Outcome a={a} busyKey={(busy) => `sq.model.busy.${busy}` as Key} />
          {!(a.outcome?.ok && a.report?.ok) && <ProbeReport report={a.report} />}
          {a.failedSave && (
            <div className="model-confirm-actions">
              <span className="field-hint">{t('sq.model.notSaved')}</span>
              <button type="button" className="btn btn-secondary" disabled={off} onClick={() => void save(true)}>
                {t('sq.model.saveAnyway')}
              </button>
            </div>
          )}

          <details className="model-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
            <summary>{t('sq.model.connection')}</summary>
            <div className="model-advanced-body">
              <Warnings items={warnings} />
              <label className="field-row">
                <span>{t('sq.model.provider')}</span>
                <select className="field" value={form.provider} disabled={off}
                        onChange={(e) => changeProvider(e.target.value as SqlProvider)}>
                  {SQL_PROVIDERS.map((name) => <option key={name} value={name}>{providerLabel(t, name)}</option>)}
                </select>
              </label>
              <label className="field-row">
                <span>{t('sq.model.baseUrl')}</span>
                <input className="field mono" value={form.baseUrl} placeholder={entry?.official_base_url ?? ''}
                       spellCheck={false} autoComplete="off" disabled={off}
                       onChange={(e) => set({ baseUrl: e.target.value })} onBlur={() => settle(form, typedKey)} />
                <small className="field-hint">{t('sq.model.baseUrl.hint', { official: entry?.official_base_url ?? '' })}</small>
              </label>
              <label className="field-row">
                <span>{t('sq.model.apiKey')}</span>
                <input className="field" type="password" autoComplete="off" spellCheck={false} value={typedKey} disabled={off}
                       placeholder={t(keepKey ? 'sq.model.apiKey.keep' : 'sq.model.apiKey.required')}
                       onChange={(e) => { a.touch(); setTypedKey(e.target.value) }} onBlur={() => settle(form, typedKey)} />
                <small className="field-hint">{t('sq.model.apiKey.hint')}</small>
              </label>
              <div className="field-row">
                <label htmlFor="sq-coder-model">{t('sq.model.coderModel')}</label>
                <Picker id="sq-coder-model" value={form.coderModel} options={options} typing={typing.coder} disabled={off}
                        same={t('sq.model.coderSame')} sameSelected={coderSame}
                        onPick={(value) => {
                          a.touch()
                          if (value === '') { setCoderSame(true); return }
                          setCoderSame(false)
                          if (value === CUSTOM) setTyping((x) => ({ ...x, coder: true }))
                          else setForm((prev) => ({ ...prev, coderModel: value }))
                        }}
                        onType={(value) => { setCoderSame(false); set({ coderModel: value }) }} />
                {typing.coder && (
                  <button type="button" className="link-btn" onClick={() => setTyping((x) => ({ ...x, coder: false }))}>
                    {t('sq.model.fromList')}
                  </button>
                )}
              </div>
              <div className="model-advanced-actions">
                <button type="button" className="btn btn-secondary" disabled={off || !hasUsableKey} onClick={() => void test()}>
                  {t('sq.model.test')}
                </button>
                {(status.source === 'page' || status.store_problem) && (
                  <button type="button" className="btn btn-ghost" disabled={off} onClick={() => { a.touch(); a.setAsking('restore') }}>
                    {t('sq.model.restore')}
                  </button>
                )}
              </div>
            </div>
          </details>
        </form>
      )}
    </Card>
  )
}

// ── Embedding ─────────────────────────────────────────────────────────────────

function EmbeddingCard({ status }: { status: SqlModelStatus }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const embedding = status.embedding
  const [form, setForm] = useState<SqlEmbeddingForm>(() => embeddingPrefill(status))
  const [typedKey, setTypedKey] = useState('')
  const [typing, setTyping] = useState(false)
  // 现在还是本地向量：要先填接口地址和 Key 才能选模型，「连接设置」默认展开
  const [open, setOpen] = useState(() => embedding.backend !== 'remote')
  const [target, setTarget] = useState<ListTarget>(() => ({ provider: 'openai', baseUrl: form.baseUrl.trim(), key: '', stamp: 0 }))
  const a = useActions()

  const keepKey = canKeepEmbeddingKey(status, form)
  const hasUsableKey = typedKey.trim() !== '' || keepKey
  const trimSlash = (value: string | undefined) => String(value ?? '').trim().replace(/\/+$/, '')
  // 现在用的是本地向量的话，换成远端本身就是改动
  const changed = embedding.backend !== 'remote'
    || trimSlash(form.baseUrl) !== trimSlash(embedding.base_url) || form.model.trim() !== embedding.model
    || String(form.dimensions).trim() !== String(embedding.dimensions) || typedKey.trim() !== ''
  const list = useModelList('embedding', target, status.editable
    && (!!target.key || canKeepEmbeddingKey(status, { baseUrl: target.baseUrl })))
  const listed = list.data?.ok ? list.data.models : []

  const settle = (next: SqlEmbeddingForm, key: string, force = false) => setTarget((prev) => (
    !force && prev.baseUrl === next.baseUrl.trim() && prev.key === key.trim()
      ? prev
      : { provider: 'openai', baseUrl: next.baseUrl.trim(), key: key.trim(), stamp: ++listStamp }
  ))
  const set = (patch: Partial<SqlEmbeddingForm>) => { a.touch(); setForm((prev) => ({ ...prev, ...patch })) }
  const adopt = (next: SqlModelStatus) => {
    queryClient.setQueryData(STATUS_KEY, next)
    const fresh = embeddingPrefill(next)
    setForm(fresh)
    setTypedKey('')
    setTyping(false)
    settle(fresh, '', true)
  }

  const save = () => a.run('save', async () => {
    const result = await sqlModelApi.saveEmbedding(embeddingRequestBody(form, typedKey))
    a.setOutcome({ ok: true, text: t('sq.model.emb.saved', {
      texts: result.rebuild?.texts ?? 0, seconds: ((result.rebuild?.duration_ms ?? 0) / 1000).toFixed(1),
    }) })
    if (result.test) a.setReport(result.test)
    adopt(result.status)
  })
  const restore = () => a.run('restore', async () => {
    const cleared = await sqlModelApi.clearEmbedding()
    a.setOutcome({ ok: true, text: t('sq.model.emb.restored') })
    adopt(cleared.status)
  })
  const test = () => a.run('test', async () => a.setReport(await sqlModelApi.testEmbedding(embeddingRequestBody(form, typedKey))))

  // 现在用的是哪个：检索索引实际在用的（active）优先——配的是远端、启动时它调不通的话，检索会退回本地向量
  const active = embedding.active?.backend ? embedding.active : embedding
  const remote = active.backend === 'remote'
  let chip: ReactNode = null
  if (embedding.fallback) chip = <Chip tone="danger" dot>{t('sq.model.emb.warn.fallback')}</Chip>
  else if (remote) chip = <Chip tone="success" dot>{t('sq.model.state.ok')}</Chip>

  const defaults = embedding.defaults
  const server = embedding.server
  const off = !!a.busy
  const body = embeddingRequestBody(form, typedKey)
  return (
    <Card title={t('sq.model.emb.title')}>
      <InUse name={remote ? <span>{active.model}</span> : t('sq.model.emb.local')} state={chip} meta={[
        t('sq.model.emb.dimensionsN', { dimensions: active.dimensions ?? embedding.dimensions }),
        remote && embedding.endpoint && <span key="host" className="mono">{hostOf(embedding.endpoint)}</span>,
        remote && embedding.key_hint !== undefined && keyText(t, embedding.key_configured, embedding.key_hint),
        !remote && t('sq.model.emb.offline'),
        embedding.saved_at !== undefined && savedText(t, embedding.source, embedding.saved_at),
      ]} />
      {embedding.fallback && (
        <div className="note danger" role="note">
          <strong>{t('sq.model.emb.warn.fallback')}</strong>
          <p>{t('sq.model.emb.warn.fallback.body')} <span className="mono">{embedding.fallback}</span></p>
        </div>
      )}
      {embedding.store_problem && (
        <Warnings items={[['sq.model.emb.warn.storeProblem', t('sq.model.emb.warn.storeProblem.body')]]} />
      )}

      {status.editable && (
        <form className="model-form" onSubmit={(e) => { e.preventDefault(); if (changed && hasUsableKey && !a.busy) a.setAsking('save') }}>
          <div className="model-pick">
            <label className="model-pick-label" htmlFor="sq-emb-model">{t('sq.model.emb.model')}</label>
            <div className="model-pick-row">
              <Picker id="sq-emb-model" value={form.model} options={listed} typing={typing} disabled={off}
                      onPick={(value) => (value === CUSTOM ? setTyping(true) : set({ model: value }))}
                      onType={(value) => set({ model: value })} />
              <button type="button" className="icon-btn" title={t('sq.model.list.refresh')} aria-label={t('sq.model.list.refresh')}
                      disabled={off || list.isFetching || !hasUsableKey} onClick={() => settle(form, typedKey, true)}>
                <Icon name="refresh" />
              </button>
              <button type="submit" className="btn btn-primary" disabled={off || !changed || !hasUsableKey}>
                {t('sq.model.save')}
              </button>
            </div>
            <p className="model-pick-source">
              {listHint(t, list, !hasUsableKey)}
              {typing && <button type="button" className="link-btn" onClick={() => setTyping(false)}>{t('sq.model.fromList')}</button>}
            </p>
          </div>

          {a.asking === 'save' && (
            <ConfirmBar confirm={t('sq.model.emb.save')} onConfirm={() => void save()} onCancel={() => a.setAsking('')}>
              {t('sq.model.emb.confirm.save', {
                address: originOf(body.base_url || defaults?.base_url) || body.base_url,
                model: body.model || defaults?.model || '',
              })}
            </ConfirmBar>
          )}
          {a.asking === 'restore' && (
            <ConfirmBar confirm={t('sq.model.confirm.restoreYes')} onConfirm={() => void restore()} onCancel={() => a.setAsking('')}>
              {t('sq.model.emb.confirm.restore', {
                target: server?.backend === 'remote'
                  ? t('sq.model.emb.remote', { model: server.model, dimensions: server.dimensions })
                  : t('sq.model.emb.local'),
              })}
            </ConfirmBar>
          )}
          <Outcome a={a} busyKey={(busy) => `sq.model.emb.busy.${busy}` as Key} />
          {!(a.outcome?.ok && a.report?.ok) && <ProbeReport report={a.report} embedding />}

          <details className="model-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
            <summary>{t('sq.model.connection')}</summary>
            <div className="model-advanced-body">
              <label className="field-row">
                <span>{t('sq.model.baseUrl')}</span>
                <input className="field mono" value={form.baseUrl} placeholder={defaults?.base_url ?? ''} spellCheck={false}
                       autoComplete="off" disabled={off}
                       onChange={(e) => set({ baseUrl: e.target.value })} onBlur={() => settle(form, typedKey)} />
                <small className="field-hint">{t('sq.model.emb.baseUrl.hint', { official: defaults?.base_url ?? '' })}</small>
              </label>
              <label className="field-row">
                <span>{t('sq.model.apiKey')}</span>
                <input className="field" type="password" autoComplete="off" spellCheck={false} value={typedKey} disabled={off}
                       placeholder={t(keepKey ? 'sq.model.emb.apiKey.keep' : 'sq.model.apiKey.required')}
                       onChange={(e) => { a.touch(); setTypedKey(e.target.value) }} onBlur={() => settle(form, typedKey)} />
                <small className="field-hint">{t('sq.model.emb.apiKey.hint')}</small>
              </label>
              <label className="field-row">
                <span>{t('sq.model.emb.dimensions')}</span>
                <input className="field" inputMode="numeric" value={form.dimensions} disabled={off}
                       placeholder={String(defaults?.dimensions ?? '')} autoComplete="off"
                       onChange={(e) => set({ dimensions: e.target.value })} />
                <small className="field-hint">{t('sq.model.emb.dimensions.hint')}</small>
              </label>
              <div className="model-advanced-actions">
                <button type="button" className="btn btn-secondary" disabled={off || !hasUsableKey} onClick={() => void test()}>
                  {t('sq.model.test')}
                </button>
                {(embedding.source === 'page' || embedding.store_problem) && (
                  <button type="button" className="btn btn-ghost" disabled={off} onClick={() => { a.touch(); a.setAsking('restore') }}>
                    {t('sq.model.restore')}
                  </button>
                )}
              </div>
            </div>
          </details>
        </form>
      )}
    </Card>
  )
}
