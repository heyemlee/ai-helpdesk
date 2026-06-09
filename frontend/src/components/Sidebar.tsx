import { useHealth } from '@/api/hooks'
import { Badge } from './ui'
import type { Settings } from '@/api/client'

export function Sidebar({ settings, onChange }: {
  settings: Settings
  onChange: (patch: Partial<Settings>) => void
}) {
  const health = useHealth(settings.apiBaseUrl)
  const routing = health.data?.routing

  return (
    <aside className="flex w-64 shrink-0 flex-col gap-4 border-r border-slate-800 bg-slate-950/40 p-4">
      <div className="flex items-center gap-2.5">
        <div className="grid h-9 w-9 place-items-center rounded-lg bg-sky-500/15
                        font-bold text-sky-300 ring-1 ring-inset ring-sky-500/25">
          EM
        </div>
        <div className="min-w-0">
          <h1 className="text-sm font-semibold text-slate-100">EchoMind Console</h1>
          <p className="truncate text-[11px] text-slate-500">工单分流 · Agent 编排 · 可观测</p>
        </div>
      </div>

      <div className="space-y-2">
        <Field label="API 地址" value={settings.apiBaseUrl}
               onChange={(v) => onChange({ apiBaseUrl: v })} placeholder="/api" />
        <Field label="用户 ID" value={settings.userId}
               onChange={(v) => onChange({ userId: v })} placeholder="u1001" />
        <Field label="会话 ID" value={settings.conversationId}
               onChange={(v) => onChange({ conversationId: v })} placeholder="自动生成" />
      </div>

      <div className="space-y-2 rounded-xl border border-slate-800 bg-slate-900/50 p-3">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold tracking-wide text-slate-400 uppercase">服务状态</span>
          <span
            className={`h-2 w-2 rounded-full ${
              health.data?.status === 'ok' ? 'bg-emerald-400' : 'bg-slate-600'
            }`}
          />
        </div>
        {health.isError && <p className="text-[11px] text-rose-300">后端不可达</p>}
        {routing && (
          <dl className="space-y-1 text-[11px]">
            <Row label="专家 Agent" value={routing.specialists.length} />
            <Row label="每类实例" value={routing.replicas_per_type} />
            <Row label="细粒度意图" value={routing.intent.fine_grained_intents} />
            <Row label="Embedding" value={routing.intent.embedding_backend} />
          </dl>
        )}
        {routing && (
          <div className="flex flex-wrap gap-1 pt-1">
            {routing.specialists.map((s) => (
              <Badge key={s} tone="info">{s}</Badge>
            ))}
            <Badge tone="muted">{routing.fallback_agent} 兜底</Badge>
          </div>
        )}
      </div>

      <a
        href={`${settings.apiBaseUrl.replace(/\/+$/, '')}/docs`}
        target="_blank"
        rel="noreferrer"
        className="mt-auto rounded-lg bg-slate-800/60 px-3 py-2 text-center text-xs text-slate-300
                   transition hover:bg-slate-700/60"
      >
        API 文档
      </a>
    </aside>
  )
}

function Field({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] text-slate-500">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5
                   text-xs text-slate-100 outline-none transition placeholder:text-slate-600
                   focus:border-sky-500/50"
      />
    </label>
  )
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="truncate font-mono text-slate-300">{value}</dd>
    </div>
  )
}
