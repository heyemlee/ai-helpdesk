import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '@/api/client'
import { useModelStatus } from '@/api/hooks'
import type {
  ChatModelBody, EmbeddingModelBody, ModelProvider, ModelSource, ModelStatus, ModelTestResult,
} from '@/api/types'
import { Card, Chip, KV, MeterRow, StateBlock } from './ui'
import { dateTime } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'

/** 输入框旁边的候选模型名，只是提示：手填任何名字都行，以测试连接的结果为准。 */
const CHAT_MODELS: Record<ModelProvider, string[]> = {
  openai: ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-terra', 'gpt-5.6-luna'],
  anthropic: ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-sonnet-5'],
}
const EMBEDDING_MODELS = ['text-embedding-3-small', 'text-embedding-3-large']

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
 * 管理与诊断 → 模型连接：对话和向量两组连接，看现状、测试、保存、恢复为服务端配置。
 *
 * Key 只往里送：输入框不预填、只在内存里、保存成功就清空；状态里只有"配没配"和末四位。
 * 能不能改由后端判（只认本机控制台发来的请求），这里只照着它给的 editable 显示表单。
 */
export function ModelSettings({ base }: { base: string }) {
  const { t } = useT()
  const status = useModelStatus(base)
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
        <ChatCard base={base} data={data} />
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

function SourceChip({ source }: { source: ModelSource }) {
  const { t } = useT()
  return source === 'page'
    ? <Chip tone="info">{t('ms.source.page')}</Chip>
    : <Chip>{t('ms.source.env')}</Chip>
}

function KeyText({ set, hint }: { set: boolean; hint: string }) {
  const { t } = useT()
  if (!set) return <>{t('ms.key.none')}</>
  return <>{hint ? t('ms.key.setHint', { hint }) : t('ms.key.set')}</>
}

function SavedRow({ at, by }: { at: string; by: string }) {
  const { t, lang } = useT()
  const when = at ? dateTime(Date.parse(at) / 1000, lang) : ''
  return <KV label={t('ms.saved')}>{[when, by].filter(Boolean).join(' · ') || t('ad.notReported')}</KV>
}

function Field({ label, hint, value, onChange, placeholder, type = 'text', list, autoComplete, disabled }: {
  label: string; hint?: string; value: string; onChange: (v: string) => void
  placeholder?: string; type?: string; list?: string; autoComplete?: string; disabled?: boolean
}) {
  return (
    <label className="field-row">
      <span>{label}</span>
      <input className="field" type={type} value={value} placeholder={placeholder} list={list}
             autoComplete={autoComplete} spellCheck={false} disabled={disabled}
             onChange={(e) => onChange(e.target.value)} />
      {hint && <small className="field-hint">{hint}</small>}
    </label>
  )
}

function TestReport({ result }: { result: ModelTestResult | null }) {
  const { t } = useT()
  if (!result) return null
  return (
    <div className={`note ${result.ok ? 'success' : 'danger'}`} role="status">
      <p>{t(result.ok ? 'ms.test.ok' : 'ms.test.failed')}</p>
      <ul className="model-checks">
        {result.checks.map((c) => (
          <li key={c.check}>
            {t(CHECK_TEXT[c.check] ?? 'ms.check.chat')}: {t(OUTCOME_TEXT[c.outcome] ?? 'ms.outcome.unexpected')}
            {c.status ? ` · HTTP ${c.status}` : ''}
            {c.latency_ms ? ` · ${c.latency_ms} ms` : ''}
            {c.detail && <> · <span className="mono">{c.detail}</span></>}
          </li>
        ))}
      </ul>
      {result.ok && result.dim ? <p>{t('ms.test.dim', { n: result.dim })}</p> : null}
      {result.warnings.includes('plaintext_http') && <p>{t('ms.warn.plaintext')}</p>}
      {result.warnings.includes('schema_not_enforced') && <p>{t('ms.warn.schema')}</p>}
    </div>
  )
}

/** 一张卡片里的测试 / 保存 / 恢复。三个动作共用一个"在忙"状态，同一时间只做一件事。 */
function useActions() {
  const { t } = useT()
  const [busy, setBusy] = useState<Busy>('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<ModelTestResult | null>(null)
  const [failedSave, setFailedSave] = useState(false)

  async function run(kind: Busy, action: () => Promise<void>) {
    setBusy(kind); setError('')
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
  return { busy, error, result, setResult, failedSave, setFailedSave, run }
}

function ChatCard({ base, data }: { base: string; data: ModelStatus }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const current = data.chat
  const [draft, setDraft] = useState<ChatModelBody>(() => ({
    provider: current.provider, base_url: current.base_url, api_key: '', model: current.model,
  }))
  const { busy, error, result, setResult, failedSave, setFailedSave, run } = useActions()
  const set = (patch: Partial<ChatModelBody>) => { setFailedSave(false); setDraft((d) => ({ ...d, ...patch })) }
  const keepKey = current.key_set && draft.provider === current.provider && sameHost(draft.base_url, current.base_url)

  const changeProvider = (provider: ModelProvider) => set(
    provider === current.provider
      ? { provider, base_url: current.base_url, model: current.model }
      : { provider, base_url: data.defaults.chat[provider].base_url, model: data.defaults.chat[provider].model },
  )
  const test = () => run('test', async () => setResult(await api.testChatModel(base, draft)))
  const save = (force = false) => run('save', async () => {
    if (!force && !window.confirm(t('ms.chat.confirm', { host: hostOf(draft.base_url) }))) return
    const saved = await api.saveChatModel(base, force ? { ...draft, force } : draft)
    queryClient.setQueryData(['model-status', base], saved)
    setResult(saved.test)
    setFailedSave(false)
    setDraft((d) => ({ ...d, api_key: '' }))
  })
  const clear = () => run('clear', async () => {
    if (!window.confirm(t('ms.chat.clearConfirm'))) return
    const restored = await api.clearChatModel(base)
    queryClient.setQueryData(['model-status', base], restored)
    setResult(null)
    setDraft({ provider: restored.chat.provider, base_url: restored.chat.base_url, api_key: '',
               model: restored.chat.model })
  })

  return (
    <Card title={t('ms.chat.title')} badge={<SourceChip source={current.source} />}>
      <p className="page-lede">{t('ms.chat.lede')}</p>
      <dl>
        <KV label={t('ms.provider')}>{current.provider === 'openai' ? 'OpenAI' : 'Anthropic'}</KV>
        <KV label={t('ms.baseUrl')}><span className="mono">{current.base_url}</span></KV>
        <KV label={t('ms.model')}><span className="mono">{current.model}</span></KV>
        <KV label={t('ms.key')}><KeyText set={current.key_set} hint={current.key_hint} /></KV>
        {current.source === 'page' && <SavedRow at={current.saved_at} by={current.saved_by} />}
      </dl>
      {data.editable && (
        <form className="settings-body model-form" onSubmit={(e) => { e.preventDefault(); void save() }}>
          <label className="field-row">
            <span>{t('ms.provider')}</span>
            <select className="field" value={draft.provider}
                    onChange={(e) => changeProvider(e.target.value as ModelProvider)}>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
            </select>
          </label>
          <Field label={t('ms.baseUrl')} value={draft.base_url} onChange={(base_url) => set({ base_url })} />
          <Field label={t('ms.key')} type="password" autoComplete="off" value={draft.api_key}
                 placeholder={t(keepKey ? 'ms.key.keep' : 'ms.key.required')}
                 onChange={(api_key) => set({ api_key })} />
          <Field label={t('ms.model')} value={draft.model} list="ms-chat-models"
                 onChange={(model) => set({ model })} />
          <datalist id="ms-chat-models">
            {CHAT_MODELS[draft.provider].map((m) => <option key={m} value={m} />)}
          </datalist>
          <TestReport result={result} />
          {error && <p className="note danger" role="alert">{error}</p>}
          <div className="settings-actions">
            {current.source === 'page' && (
              <button type="button" className="btn btn-ghost" disabled={!!busy} onClick={() => void clear()}>
                {t('ms.restore')}
              </button>
            )}
            <button type="button" className="btn btn-secondary" disabled={!!busy} onClick={() => void test()}>
              {busy === 'test' ? t('ms.testing') : t('ms.test')}
            </button>
            {failedSave && (
              <button type="button" className="btn btn-secondary" disabled={!!busy} onClick={() => void save(true)}>
                {t('ms.saveAnyway')}
              </button>
            )}
            <button type="submit" className="btn btn-primary" disabled={!!busy}>
              {busy === 'save' ? t('ms.saving') : t('ms.save')}
            </button>
          </div>
        </form>
      )}
    </Card>
  )
}

function EmbeddingCard({ base, data }: { base: string; data: ModelStatus }) {
  const { t } = useT()
  const queryClient = useQueryClient()
  const current = data.embedding
  const job = data.switch
  const running = job.state === 'running'
  const [draft, setDraft] = useState<EmbeddingModelBody>(() => ({
    base_url: current.backend === 'remote' ? current.base_url : data.defaults.embedding.base_url,
    api_key: '',
    model: current.backend === 'remote' ? current.model : data.defaults.embedding.model,
    dim: 0,
    query_prefix: current.query_prefix,
  }))
  const { busy, error, result, setResult, run } = useActions()
  const set = (patch: Partial<EmbeddingModelBody>) => setDraft((d) => ({ ...d, ...patch }))
  const keepKey = current.key_set && sameHost(draft.base_url, current.base_url)

  const test = () => run('test', async () => setResult(await api.testEmbeddingModel(base, draft)))
  const save = () => run('save', async () => {
    if (!window.confirm(t('ms.emb.confirm', { model: draft.model }))) return
    const started = await api.saveEmbeddingModel(base, draft)
    queryClient.setQueryData(['model-status', base], started)
    setResult(started.test)
    setDraft((d) => ({ ...d, api_key: '' }))
  })
  const clear = () => run('clear', async () => {
    if (!window.confirm(t('ms.emb.clearConfirm'))) return
    const restored = await api.clearEmbeddingModel(base)
    queryClient.setQueryData(['model-status', base], restored)
    setResult(null)
  })

  return (
    <Card title={t('ms.emb.title')} badge={<SourceChip source={current.source} />}>
      <p className="page-lede">{t('ms.emb.lede')}</p>
      <dl>
        <KV label={t('ms.emb.backend')}>
          {t(current.backend === 'remote' ? 'ms.emb.remote' : 'ms.emb.local')}
          {current.backend === 'remote' && !current.available && <> · {t('ms.emb.cooling')}</>}
        </KV>
        {current.backend === 'remote' && (
          <KV label={t('ms.baseUrl')}><span className="mono">{current.base_url}</span></KV>
        )}
        <KV label={t('ms.model')}><span className="mono">{current.model}</span></KV>
        <KV label={t('ms.emb.dim')}>{current.dim}</KV>
        {current.backend === 'remote' && (
          <KV label={t('ms.key')}><KeyText set={current.key_set} hint={current.key_hint} /></KV>
        )}
        {current.source === 'page' && <SavedRow at={current.saved_at} by={current.saved_by} />}
      </dl>

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
        <form className="settings-body model-form" onSubmit={(e) => { e.preventDefault(); void save() }}>
          <Field label={t('ms.baseUrl')} value={draft.base_url} disabled={running}
                 onChange={(base_url) => set({ base_url })} />
          <Field label={t('ms.key')} type="password" autoComplete="off" value={draft.api_key} disabled={running}
                 placeholder={t(keepKey ? 'ms.key.keep' : 'ms.key.required')}
                 onChange={(api_key) => set({ api_key })} />
          <Field label={t('ms.model')} value={draft.model} list="ms-embedding-models" disabled={running}
                 onChange={(model) => set({ model })} />
          <datalist id="ms-embedding-models">
            {EMBEDDING_MODELS.map((m) => <option key={m} value={m} />)}
          </datalist>
          <Field label={t('ms.emb.dim')} hint={t('ms.emb.dim.hint')} type="number" disabled={running}
                 value={draft.dim ? String(draft.dim) : ''}
                 onChange={(v) => set({ dim: Number.parseInt(v, 10) || 0 })} />
          <label className="field-row">
            <span>{t('ms.emb.prefix')}</span>
            <textarea className="field" rows={2} value={draft.query_prefix} disabled={running} spellCheck={false}
                      onChange={(e) => set({ query_prefix: e.target.value })} />
            <small className="field-hint">{t('ms.emb.prefix.hint')}</small>
          </label>
          <TestReport result={result} />
          {error && <p className="note danger" role="alert">{error}</p>}
          <div className="settings-actions">
            {current.source === 'page' && (
              <button type="button" className="btn btn-ghost" disabled={!!busy || running}
                      onClick={() => void clear()}>
                {t('ms.restore')}
              </button>
            )}
            <button type="button" className="btn btn-secondary" disabled={!!busy || running}
                    onClick={() => void test()}>
              {busy === 'test' ? t('ms.testing') : t('ms.test')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={!!busy || running}>
              {busy === 'save' ? t('ms.saving') : t('ms.emb.save')}
            </button>
          </div>
        </form>
      )}
    </Card>
  )
}
