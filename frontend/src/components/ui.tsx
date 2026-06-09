import type { ReactNode } from 'react'

type Tone = 'good' | 'warn' | 'bad' | 'info' | 'muted'

const TONE_CLASS: Record<Tone, string> = {
  good: 'bg-emerald-500/12 text-emerald-300 ring-emerald-500/25',
  warn: 'bg-amber-500/12 text-amber-300 ring-amber-500/25',
  bad: 'bg-rose-500/12 text-rose-300 ring-rose-500/25',
  info: 'bg-sky-500/12 text-sky-300 ring-sky-500/25',
  muted: 'bg-slate-500/12 text-slate-300 ring-slate-500/25',
}

export function Badge({ tone = 'muted', children, title }: {
  tone?: Tone; children: ReactNode; title?: string
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]
                  font-medium ring-1 ring-inset whitespace-nowrap ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  )
}

export function Panel({ title, badge, actions, children, className = '' }: {
  title: string; badge?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={`rounded-xl border border-slate-800 bg-slate-900/60 ${className}`}>
      <header className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          {badge}
        </div>
        {actions}
      </header>
      <div className="p-4">{children}</div>
    </section>
  )
}

/** 0-1 的分数条。用于领域分、置信度、成功率。 */
export function ScoreBar({ value, tone = 'info', label }: {
  value: number; tone?: Tone; label?: string
}) {
  const fill: Record<Tone, string> = {
    good: 'bg-emerald-400', warn: 'bg-amber-400', bad: 'bg-rose-400',
    info: 'bg-sky-400', muted: 'bg-slate-500',
  }
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${fill[tone]}`}
          style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
        />
      </div>
      {label && <span className="w-11 text-right font-mono text-[11px] text-slate-400">{label}</span>}
    </div>
  )
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-xs text-slate-500">{children}</p>
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return (
    <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300 ring-1 ring-inset ring-rose-500/20">
      {message}
    </p>
  )
}
