import { Fragment, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '@/api/client'
import { useHealth, useModelStatus } from '@/api/hooks'
import type {
  ChatModelBody, EmbeddingModelBody, HealthResponse, ModelListResult, ModelProvider, ModelStatus,
  ModelTestResult,
} from '@/api/types'
import { Card, Chip, MeterRow, StateBlock } from './ui'
import { Icon } from './Icon'
import { dateTime } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { llmReasonKey } from '@/lib/llm'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'

/** 接口读不出模型列表时，下拉框里放的几个常见名字。只是候选：以测试连接的结果为准。 */
const CHAT_MODELS: Record<ModelProvider, string[]> = {
  openai: ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-terra', 'gpt-5.6-luna'],
  anthropic: ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-sonnet-5'],
}
const EMBEDDING_MODELS = ['text-embedding-3-small', 'text-embedding-3-large']

/** 下拉框里「手动输入」那一项的值。模型名不能以空格开头，撞不上真实的名字。 */
const CUSTOM = ' custom'

/** 后端拒绝时给的代号 → 文案。没收录的代号显示后端的原话。 */
const ERROR_TEXT: Record<string, Key> = {
  invalid_base_url: 'ms.err.invalid_base_url',
  forbidden_address: 'ms.err.forbidden_address',
  invalid_api_key: 'ms.err.invalid_api_key',
  invalid_model: 'ms.err.invalid_model',
  invalid_dim: 'ms.err.invalid_dim',
  invalid_query_prefix: 'ms.err.invalid_query_prefix',
  api_key_required: 'ms.err.api_key_required',
  switch_in_progress: 'ms.err.switch_in_progress',
  env_incomplete: 'ms.err.env_incomplete',
  not_local: 'ms.reason.not_local',
  cross_origin: 'ms.reason.cross_origin',
  read_only_instance: 'ms.reason.read_only_instance',
  disabled: 'ms.reason.disabled',
}

const REASON_TEXT: Record<string, Key> = {
  disabled: 'ms.reason.disabled',
  read_only_instance: 'ms.reason.read_only_instance',
  not_local: 'ms.reason.not_local',
  cross_origin: 'ms.reason.cross_origin',
}

const OUTCOME_TEXT: Record<string, Key> = {
  ok: 'ms.outcome.ok',
  auth_failed: 'ms.outcome.auth_failed',
  permission_denied: 'ms.outcome.permission_denied',
  quota_exhausted: 'ms.outcome.quota_exhausted',
  rate_limited: 'ms.outcome.rate_limited',
  model_not_found: 'ms.outcome.model_not_found',
  bad_request: 'ms.outcome.bad_request',
  timeout: 'ms.outcome.timeout',
  connection_failed: 'ms.outcome.connection_failed',
  server_error: 'ms.outcome.server_error',
  empty_response: 'ms.outcome.empty_response',
  not_structured: 'ms.outcome.not_structured',
  not_api_response: 'ms.outcome.not_api_response',
  dim_mismatch: 'ms.outcome.dim_mismatch',
  list_unsupported: 'ms.outcome.list_unsupported',
  skipped: 'ms.outcome.skipped',
  unexpected: 'ms.outcome.unexpected',
}

const CHECK_TEXT: Record<string, Key> = {
  chat: 'ms.check.chat',
  structured: 'ms.check.structured',
  embedding: 'ms.check.embedding',
}

function hostOf(url: string): string {
  try { return new URL(url).host } catch { return url }
}

/** 同一台主机才允许不重填 Key——和后端 same_address 同一个口径。 */
function sameHost(a: string, b: string): boolean {
  try {
    const x = new URL(a); const y = new URL(b)
    return x.protocol === y.protocol && x.host === y.host
  } catch { return false }
}

/** 后端存地址时去掉了结尾的斜杠，比较「改没改」时也一样。 */
const sameUrl = (a: string, b: string) => a.trim().replace(/\/+$/, '') === b.trim().replace(/\/+$/, '')

/** 去重并按字母排；空串不要。 */
const choicesOf = (...names: string[]) =>
  [...new Set(names.filter(Boolean))].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))

type Busy = '' | 'test' | 'save' | 'clear'

/** 后端的拒绝：要么是带代号的结构化错误（可能附着测试结果），要么是一般的接口错误。 */
function readFailure(error: unknown): { code: string; message: string; test?: ModelTestResult } {
  if (error instanceof ApiError && error.detail && typeof error.detail === 'object') {
    const detail = error.detail as { code?: string; message?: string; test?: ModelTestResult }
    return { code: detail.code ?? '', message: detail.message ?? error.message, test: detail.test }
  }
  return { code: '', message: error instanceof Error ? error.message : String(error) }
}

/**
 * 管理与诊断 → 模型连接。
 *
 * 每组一张卡片：最上面写清楚现在用的是哪个模型，下面一个下拉框换模型、一个保存键。
 * 接口地址、Key 这些不常改的收在「连接设置」里。下拉框的候选是问接口要来的
 * （POST /admin/model/{chat,embedding}/models），读不到就退回手动输入。
 *
 * Key 只往里送：输入框不预填、只在内存里、保存成功就清空；状态里只有"配没配"和末四位。
 * 能不能改由后端判（只认本机控制台发来的请求），这里只照着它给的 editable 显示表单。
 * 确认一律在页面里做，不用 window.confirm：有的内嵌浏览器不弹它，保存会被悄悄取消。
 */
export function ModelSettings({ base }: { base: string }) {
  const { t } = useT()
  const status = useModelStatus(base)
  const health = useHealth(base).data
  const data = status.data

  let body
  if (status.isError) {
    const { body: text, detail } = explainError(status.error, t)
    body = <StateBlock tone="danger" title={t('ms.loadFailed')} detail={detail}>{text}</StateBlock>
  } else if (!data) {
    body = <StateBlock busy title={t('ms.loading')} />
  } else {
    const problems = Object.keys(data.problems ?? {})
    body = (
      <>
        {!data.editable && (
          <p className={`note ${data.enabled ? 'caution' : 'neutral'}`} role="note">
            {t(REASON_TEXT[data.reason] ?? 'ms.reason.disabled')}
          </p>
        )}
        {problems.length > 0 && (
          <p className="note caution" role="note">{t('ms.problems', { sections: problems.join(', ') })}</p>
        )}
        <ChatCard base={base} data={data} llm={health?.llm} />
        <EmbeddingCard base={base} data={data} />
      </>
    )
  }

  return (
    <div className="page scroll"><div className="page-inner">
      <div>
        <h2 className="page-title">{t('ms.title')}</h2>
        <p className="page-lede">{t('ms.lede')}</p>
      </div>
      {body}
    </div></div>
  )
}

// ── 现在用的是哪个模型 ────────────────────────────────────────────────────────

/** 模型名放大，接口、Key、来源放在下面一行小字。 */
function InUse({ name, state, meta }: { name: ReactNode; state?: ReactNode; meta: ReactNode[] }) {
  const { t } = useT()
  return (
    <div className="model-now">
      <span className="model-now-label">{t('ms.inUse')}</span>
      <div className="model-now-main">
        <strong className="model-now-name">{name}</strong>
        {state}
      </div>
      <p className="model-now-meta">
        {meta.filter(Boolean).map((part, i) => (
          <Fragment key={i}>
            {/* 不换行空格把点粘在前一项后面：窄屏折行时点留在行尾，不会跑到下一行开头 */}
            {i > 0 && <span className="model-now-sep" aria-hidden="true">{'\u00a0· '}</span>}
            <span>{part}</span>
          </Fragment>
        ))}
      </p>
    </div>
  )
}

function keyText(t: ReturnType<typeof useT>['t'], set: boolean, hint: string): string {
  if (!set) return t('ms.key.none')
  return hint ? t('ms.key.ends', { hint }) : t('ms.key.set')
}

function SourceText({ source, at, by }: { source: 'page' | 'env'; at: string; by: string }) {
  const { t, lang } = useT()
  if (source !== 'page') return <>{t('ms.source.env')}</>
  const when = at ? dateTime(Date.parse(at) / 1000, lang) : ''
  return <span title={by || undefined}>{when ? t('ms.source.pageAt', { when }) : t('ms.source.page')}</span>
}

// ── 下拉框 ────────────────────────────────────────────────────────────────────

/** 每次「连接定下来」给一个新编号。放模块级：卸载再挂上也不会和缓存里旧的那份撞号。 */
let listStamp = 0
const nextStamp = () => ++listStamp

/** 下拉框的候选从哪个连接读。Key 只在组件状态里，不进查询缓存的键。 */
interface ListTarget { provider: ModelProvider; base_url: string; api_key: string; stamp: number }

function useModelList(base: string, section: 'chat' | 'embedding', target: ListTarget, enabled: boolean) {
  return useQuery<ModelListResult>({
    queryKey: ['model-list', base, section, target.provider, target.base_url, target.stamp],
    queryFn: () => (section === 'chat'
      ? api.listChatModels(base, { provider: target.provider, base_url: target.base_url, api_key: target.api_key })
      : api.listEmbeddingModels(base, { base_url: target.base_url, api_key: target.api_key })),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  })
}

/**
 * 选模型的那一行：下拉框 + 重新读取 + 保存。最后一项「其他」换成输入框，列表里没有的模型照样能填。
 * 下面一行小字说候选是从哪来的：接口报了几个、读不到的原因，或者要先填 Key。
 */
function ModelPicker({ id, value, fallback, list, needKey, disabled, onChange, onRefresh, action }: {
  id: string
  value: string
  /** 读不到接口的列表时给的常见名字 */
  fallback: string[]
  list: ReturnType<typeof useModelList>
  needKey: boolean
  disabled: boolean
  onChange: (model: string) => void
  onRefresh: () => void
  action: ReactNode
}) {
  const { t } = useT()
  const [typing, setTyping] = useState(false)
  const result = list.data
  // 读到了用接口报的；正在读时只放当前这个，免得常见名字闪一下又被换掉
  const options = choicesOf(...(result?.ok ? result.models : list.isFetching ? [] : fallback), value)

  let source: ReactNode = null
  if (needKey) source = t('ms.list.needKey')
  else if (list.isFetching) source = t('ms.list.loading')
  else if (list.isError) {
    const failure = readFailure(list.error)
    source = t('ms.list.failed', {
      reason: failure.code && ERROR_TEXT[failure.code] ? t(ERROR_TEXT[failure.code]!) : failure.message,
    })
  } else if (result && !result.ok) {
    source = t('ms.list.failed', { reason: t(OUTCOME_TEXT[result.outcome] ?? 'ms.outcome.unexpected') })
  } else if (result) source = t('ms.list.count', { n: result.models.length })

  return (
    <div className="model-pick">
      <label className="model-pick-label" htmlFor={id}>{t('ms.model')}</label>
      <div className="model-pick-row">
        {typing ? (
          <input id={id} className="field" value={value} disabled={disabled} spellCheck={false}
                 autoComplete="off" placeholder={t('ms.model.typeName')} autoFocus
                 onChange={(e) => onChange(e.target.value)} />
        ) : (
          <select id={id} className="field" value={value} disabled={disabled}
                  onChange={(e) => (e.target.value === CUSTOM ? setTyping(true) : onChange(e.target.value))}>
            {!value && <option value="" disabled>{t('ms.model.choose')}</option>}
            {options.map((m) => <option key={m} value={m}>{m}</option>)}
            <option value={CUSTOM}>{t('ms.model.custom')}</option>
          </select>
        )}
        <button type="button" className="icon-btn" title={t('ms.list.refresh')} aria-label={t('ms.list.refresh')}
                disabled={disabled || needKey || list.isFetching} onClick={onRefresh}>
          <Icon name="refresh" />
        </button>
        {action}
      </div>
      <p className="model-pick-source">
        {source}
        {typing && (
          <button type="button" className="link-btn" onClick={() => setTyping(false)}>{t('ms.model.fromList')}</button>
        )}
      </p>
    </div>
  )
}

// ── 测试结果、确认 ────────────────────────────────────────────────────────────

function TestReport({ result, saved }: { result: ModelTestResult | null; saved?: string }) {
  const { t } = useT()
  if (!result) return null
  const failed = result.checks.filter((c) => c.outcome !== 'ok')
  const latency = result.checks.reduce((sum, c) => sum + (c.latency_ms || 0), 0)
  let headline = t(result.ok ? 'ms.test.ok' : 'ms.test.failed')
  if (saved) headline = result.ok ? t('ms.saved.ok', { model: saved }) : t('ms.saved.forced', { model: saved })
  return (
    <div className={`note ${result.ok ? 'success' : 'danger'}`} role="status">
      <p>
        {result.ok && <Icon name="check" size={14} />}
        {headline}
        {result.ok && latency > 0 && <span className="model-latency"> · {t('ms.test.latency', { ms: latency })}</span>}
      </p>
      {failed.length > 0 && (
        <ul className="model-checks">
          {failed.map((c) => (
            <li key={c.check}>
              {t(CHECK_TEXT[c.check] ?? 'ms.check.chat')}: {t(OUTCOME_TEXT[c.outcome] ?? 'ms.outcome.unexpected')}
              {c.status ? ` · HTTP ${c.status}` : ''}
              {c.detail && <> · <span className="mono">{c.detail}</span></>}
            </li>
          ))}
        </ul>
      )}
      {result.ok && result.dim ? <p>{t('ms.test.dim', { n: result.dim })}</p> : null}
      {result.warnings.includes('plaintext_http') && <p className="model-warn">{t('ms.warn.plaintext')}</p>}
      {result.warnings.includes('schema_not_enforced') && <p className="model-warn">{t('ms.warn.schema')}</p>}
    </div>
  )
}

/** 页面里的确认条，代替 window.confirm。 */
function ConfirmBar({ children, confirm, onConfirm, onCancel }: {
  children: ReactNode; confirm: string; onConfirm: () => void; onCancel: () => void
}) {
  const { t } = useT()
  return (
    <div className="note caution model-confirm" role="alertdialog" aria-label={confirm}>
      <p>{children}</p>
      <div className="model-confirm-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>{t('ms.cancel')}</button>
        <button type="button" className="btn btn-primary" onClick={onConfirm}>{confirm}</button>
      </div>
    </div>
  )
}

/** 一张卡片里的测试 / 保存 / 恢复。三个动作共用一个"在忙"状态，同一时间只做一件事。 */
function useActions() {
  const { t } = useT()
  const [busy, setBusy] = useState<Busy>('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<ModelTestResult | null>(null)
  const [saved, setSaved] = useState('')
  const [failedSave, setFailedSave] = useState(false)
  const [asking, setAsking] = useState<'' | 'save' | 'clear'>('')

  async function run(kind: Busy, action: () => Promise<void>) {
    setBusy(kind); setError(''); setAsking(''); setSaved('')
    try {
      await action()
    } catch (err) {
      const failure = readFailure(err)
      if (failure.code === 'test_failed' && failure.test) {
        setResult(failure.test)
        setFailedSave(true)
      } else {
        setError(failure.code && ERROR_TEXT[failure.code] ? t(ERROR_TEXT[failure.code]!) : failure.message)
      }
    } finally {
      setBusy('')
    }
  }
  /** 表单一改，上一次的结果和确认就作废。 */
  const touch = () => { setFailedSave(false); setAsking(''); setSaved('') }
  return { busy, error, result, setResult, saved, setSaved, failedSave, setFailedSave, asking, setAsking, run, touch }
}

// ── 对话模型 ──────────────────────────────────────────────────────────────────

function ChatCard({ base, data, llm }: { base: string; data: ModelStatus; llm: HealthResponse['llm'] }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const current = data.chat
  const [draft, setDraft] = useState<ChatModelBody>(() => ({
    provider: current.provider, base_url: current.base_url, api_key: '', model: current.model,
  }))
  const [target, setTarget] = useState<ListTarget>(() => ({
    provider: current.provider, base_url: current.base_url, api_key: '', stamp: 0,
  }))
  const [open, setOpen] = useState(() => !current.key_set)
  const a = useActions()

  const canKeep = (provider: ModelProvider, url: string) =>
    current.key_set && provider === current.provider && sameHost(url, current.base_url)
  const keepKey = canKeep(draft.provider, draft.base_url)
  const hasKey = keepKey || draft.api_key.trim() !== ''
  const list = useModelList(base, 'chat', target, data.editable && (!!target.api_key || canKeep(target.provider, target.base_url)))
  const changed = draft.provider !== current.provider || !sameUrl(draft.base_url, current.base_url)
    || draft.model.trim() !== current.model || draft.api_key.trim() !== ''
  const newHost = draft.provider !== current.provider || !sameHost(draft.base_url, current.base_url)

  const set = (patch: Partial<ChatModelBody>) => { a.touch(); setDraft((d) => ({ ...d, ...patch })) }
  /** 连接定下来了（换了协议、地址或 Key 输入框失焦、点了重新读取）：按它重新读候选。没变就不读。 */
  const settle = (next: ChatModelBody, force = false) => setTarget((prev) => (
    !force && prev.provider === next.provider && sameUrl(prev.base_url, next.base_url) && prev.api_key === next.api_key
      ? prev
      : { provider: next.provider, base_url: next.base_url, api_key: next.api_key, stamp: nextStamp() }
  ))
  const changeProvider = (provider: ModelProvider) => {
    const next = provider === current.provider
      ? { ...draft, provider, base_url: current.base_url, model: current.model }
      : { ...draft, provider, base_url: data.defaults.chat[provider].base_url, model: data.defaults.chat[provider].model }
    set(next); settle(next)
  }

  const test = () => a.run('test', async () => a.setResult(await api.testChatModel(base, draft)))
  const save = (force = false) => a.run('save', async () => {
    const saved = await api.saveChatModel(base, force ? { ...draft, force } : draft)
    queryClient.setQueryData(['model-status', base], saved)
    void queryClient.invalidateQueries({ queryKey: ['health', base] })
    a.setResult(saved.test)
    a.setSaved(saved.chat.model)
    a.setFailedSave(false)
    setDraft((d) => ({ ...d, api_key: '' }))
    setTarget((prev) => ({ ...prev, api_key: '' }))
  })
  const submit = () => {
    if (!changed || !hasKey || a.busy) return
    if (newHost) a.setAsking('save')
    else void save()
  }
  const clear = () => a.run('clear', async () => {
    const restored = await api.clearChatModel(base)
    queryClient.setQueryData(['model-status', base], restored)
    void queryClient.invalidateQueries({ queryKey: ['health', base] })
    a.setResult(null)
    const next = { provider: restored.chat.provider, base_url: restored.chat.base_url, api_key: '',
                   model: restored.chat.model }
    setDraft(next); settle(next)
  })

  const blocked = llm?.available === false
  const state = llm?.available === undefined ? null : blocked
    ? <Chip tone="danger" dot>{t('ms.state.blocked')}</Chip>
    : <Chip tone="success" dot>{t('ms.state.ok')}</Chip>

  return (
    <Card title={t('ms.chat.title')}>
      <InUse name={<span>{current.model}</span>} state={state} meta={[
        t(current.provider === 'openai' ? 'ms.protocol.openai' : 'ms.protocol.anthropic'),
        <span key="host" className="mono">{hostOf(current.base_url)}</span>,
        keyText(t, current.key_set, current.key_hint),
        <SourceText key="src" source={current.source} at={current.saved_at} by={current.saved_by} />,
      ]} />
      {blocked && (
        <p className="note danger" role="note">
          {t('ms.state.blockedWhy', { reason: t(llmReasonKey(llm?.unavailable_reason)) })}
        </p>
      )}

      {data.editable && (
        <form className="model-form" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <ModelPicker id="ms-chat-model" value={draft.model} disabled={!!a.busy}
                       fallback={CHAT_MODELS[draft.provider]}
                       list={list} needKey={!hasKey}
                       onChange={(model) => set({ model })} onRefresh={() => settle(draft, true)}
                       action={(
                         <button type="submit" className="btn btn-primary" aria-busy={a.busy === 'save'}
                                 disabled={!!a.busy || !changed || !hasKey}>
                           {a.busy === 'save' ? t('ms.saving') : t('ms.save')}
                         </button>
                       )} />

          {a.asking === 'save' && (
            <ConfirmBar confirm={t('ms.confirm.save')} onConfirm={() => void save()} onCancel={() => a.setAsking('')}>
              {t('ms.chat.confirm', { host: hostOf(draft.base_url) })}
            </ConfirmBar>
          )}
          {a.asking === 'clear' && (
            <ConfirmBar confirm={t('ms.confirm.restore')} onConfirm={() => void clear()} onCancel={() => a.setAsking('')}>
              {t('ms.chat.clearConfirm')}
            </ConfirmBar>
          )}
          <TestReport result={a.result} saved={a.saved} />
          {a.error && <p className="note danger" role="alert">{a.error}</p>}
          {a.failedSave && (
            <div className="model-confirm-actions">
              <button type="button" className="btn btn-secondary" disabled={!!a.busy} onClick={() => void save(true)}>
                {t('ms.saveAnyway')}
              </button>
            </div>
          )}

          <details className="model-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
            <summary>{t('ms.advanced')}</summary>
            <div className="model-advanced-body">
              <label className="field-row">
                <span>{t('ms.provider')}</span>
                <select className="field" value={draft.provider} disabled={!!a.busy}
                        onChange={(e) => changeProvider(e.target.value as ModelProvider)}>
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </label>
              <label className="field-row">
                <span>{t('ms.baseUrl')}</span>
                <input className="field mono" value={draft.base_url} spellCheck={false} disabled={!!a.busy}
                       onChange={(e) => set({ base_url: e.target.value })} onBlur={() => settle(draft)} />
              </label>
              <label className="field-row">
                <span>{t('ms.key')}</span>
                <input className="field" type="password" autoComplete="off" value={draft.api_key} disabled={!!a.busy}
                       placeholder={t(keepKey ? 'ms.key.keep' : 'ms.key.required')}
                       onChange={(e) => set({ api_key: e.target.value })} onBlur={() => settle(draft)} />
              </label>
              <div className="model-advanced-actions">
                <button type="button" className="btn btn-secondary" disabled={!!a.busy || !hasKey}
                        aria-busy={a.busy === 'test'} onClick={() => void test()}>
                  {a.busy === 'test' ? t('ms.testing') : t('ms.test')}
                </button>
                {current.source === 'page' && (
                  <button type="button" className="btn btn-ghost" disabled={!!a.busy}
                          onClick={() => { a.touch(); a.setAsking('clear') }}>
                    {t('ms.restore')}
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

// ── 向量模型 ──────────────────────────────────────────────────────────────────

function EmbeddingCard({ base, data }: { base: string; data: ModelStatus }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const current = data.embedding
  const job = data.switch
  const running = job.state === 'running'
  const remote = current.backend === 'remote'
  const [draft, setDraft] = useState<EmbeddingModelBody>(() => ({
    base_url: remote ? current.base_url : data.defaults.embedding.base_url,
    api_key: '',
    model: remote ? current.model : data.defaults.embedding.model,
    dim: 0,
    query_prefix: current.query_prefix,
  }))
  const [target, setTarget] = useState<ListTarget>(() => ({
    provider: 'openai', base_url: remote ? current.base_url : data.defaults.embedding.base_url, api_key: '', stamp: 0,
  }))
  const [open, setOpen] = useState(() => !remote)
  const a = useActions()

  const canKeep = (url: string) => current.key_set && sameHost(url, current.base_url)
  const keepKey = canKeep(draft.base_url)
  const hasKey = keepKey || draft.api_key.trim() !== ''
  const list = useModelList(base, 'embedding', target, data.editable && (!!target.api_key || canKeep(target.base_url)))
  const changed = !remote || draft.model.trim() !== current.model || !sameUrl(draft.base_url, current.base_url)
    || draft.api_key.trim() !== '' || draft.dim !== 0 || draft.query_prefix !== current.query_prefix

  const set = (patch: Partial<EmbeddingModelBody>) => { a.touch(); setDraft((d) => ({ ...d, ...patch })) }
  const settle = (next: EmbeddingModelBody, force = false) => setTarget((prev) => (
    !force && sameUrl(prev.base_url, next.base_url) && prev.api_key === next.api_key
      ? prev
      : { provider: 'openai', base_url: next.base_url, api_key: next.api_key, stamp: nextStamp() }
  ))

  const test = () => a.run('test', async () => a.setResult(await api.testEmbeddingModel(base, draft)))
  const save = () => a.run('save', async () => {
    const started = await api.saveEmbeddingModel(base, draft)
    queryClient.setQueryData(['model-status', base], started)
    a.setResult(started.test)
    setDraft((d) => ({ ...d, api_key: '' }))
    setTarget((prev) => ({ ...prev, api_key: '' }))
  })
  const submit = () => {
    if (!changed || !hasKey || a.busy || running) return
    a.setAsking('save')
  }
  const clear = () => a.run('clear', async () => {
    const restored = await api.clearEmbeddingModel(base)
    queryClient.setQueryData(['model-status', base], restored)
    a.setResult(null)
  })

  let state: ReactNode = null
  if (remote) {
    state = current.available
      ? <Chip tone="success" dot>{t('ms.state.ok')}</Chip>
      : <Chip tone="caution" dot>{t('ms.emb.cooling')}</Chip>
  }

  return (
    <Card title={t('ms.emb.title')}>
      <InUse name={remote ? <span>{current.model}</span> : t('ms.emb.local')} state={state} meta={[
        t('ms.emb.dimN', { n: current.dim }),
        remote && <span key="host" className="mono">{hostOf(current.base_url)}</span>,
        remote && keyText(t, current.key_set, current.key_hint),
        remote ? <SourceText key="src" source={current.source} at={current.saved_at} by={current.saved_by} />
               : t('ms.emb.offline'),
      ]} />

      {running && (
        <div className="model-switch" role="status">
          <MeterRow label={t(job.restoring ? 'ms.emb.restoring' : 'ms.emb.rebuilding')}
                    value={job.total ? job.done / job.total : 0}
                    note={t('ms.emb.progress', { done: job.done, total: job.total })} />
        </div>
      )}
      {job.state === 'failed' && (
        <p className="note danger" role="alert">{t('ms.emb.failed', { error: job.error })}</p>
      )}
      {job.state === 'succeeded' && (
        <p className="note success" role="status">
          {t(job.restoring ? 'ms.emb.restored' : 'ms.emb.switched', { n: job.total })}
        </p>
      )}

      {data.editable && (
        <form className="model-form" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <ModelPicker id="ms-embedding-model" value={draft.model} disabled={!!a.busy || running}
                       fallback={EMBEDDING_MODELS}
                       list={list} needKey={!hasKey}
                       onChange={(model) => set({ model })} onRefresh={() => settle(draft, true)}
                       action={(
                         <button type="submit" className="btn btn-primary" aria-busy={a.busy === 'save'}
                                 disabled={!!a.busy || running || !changed || !hasKey}>
                           {a.busy === 'save' ? t('ms.saving') : t('ms.save')}
                         </button>
                       )} />

          {a.asking === 'save' && (
            <ConfirmBar confirm={t('ms.confirm.switch')} onConfirm={() => void save()} onCancel={() => a.setAsking('')}>
              {t('ms.emb.confirm', { model: draft.model })}
            </ConfirmBar>
          )}
          {a.asking === 'clear' && (
            <ConfirmBar confirm={t('ms.confirm.restore')} onConfirm={() => void clear()} onCancel={() => a.setAsking('')}>
              {t('ms.emb.clearConfirm')}
            </ConfirmBar>
          )}
          <TestReport result={a.result} />
          {a.error && <p className="note danger" role="alert">{a.error}</p>}

          <details className="model-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
            <summary>{t('ms.advanced')}</summary>
            <div className="model-advanced-body">
              <label className="field-row">
                <span>{t('ms.baseUrl')}</span>
                <input className="field mono" value={draft.base_url} spellCheck={false} disabled={!!a.busy || running}
                       onChange={(e) => set({ base_url: e.target.value })} onBlur={() => settle(draft)} />
              </label>
              <label className="field-row">
                <span>{t('ms.key')}</span>
                <input className="field" type="password" autoComplete="off" value={draft.api_key}
                       disabled={!!a.busy || running}
                       placeholder={t(keepKey ? 'ms.key.keep' : 'ms.key.required')}
                       onChange={(e) => set({ api_key: e.target.value })} onBlur={() => settle(draft)} />
              </label>
              <label className="field-row">
                <span>{t('ms.emb.dim')}</span>
                <input className="field" type="number" min={1} value={draft.dim ? String(draft.dim) : ''}
                       placeholder={t('ms.emb.dim.auto')} disabled={!!a.busy || running}
                       onChange={(e) => set({ dim: Number.parseInt(e.target.value, 10) || 0 })} />
              </label>
              <label className="field-row">
                <span>{t('ms.emb.prefix')}</span>
                <textarea className="field" rows={2} value={draft.query_prefix} disabled={!!a.busy || running}
                          spellCheck={false} onChange={(e) => set({ query_prefix: e.target.value })} />
                <small className="field-hint">{t('ms.emb.prefix.hint')}</small>
              </label>
              <div className="model-advanced-actions">
                <button type="button" className="btn btn-secondary" disabled={!!a.busy || running || !hasKey}
                        aria-busy={a.busy === 'test'} onClick={() => void test()}>
                  {a.busy === 'test' ? t('ms.testing') : t('ms.test')}
                </button>
                {current.source === 'page' && (
                  <button type="button" className="btn btn-ghost" disabled={!!a.busy || running}
                          onClick={() => { a.touch(); a.setAsking('clear') }}>
                    {t('ms.restore')}
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
