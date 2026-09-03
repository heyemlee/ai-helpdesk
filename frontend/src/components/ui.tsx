import type { ReactNode } from 'react'

/** 语义色只用在需要被读懂的数据上；主操作与选中态一律无彩。 */
export type Tone = 'neutral' | 'info' | 'success' | 'caution' | 'warning' | 'danger'

export function Chip({ tone = 'neutral', mono, dot, children, title }: {
  tone?: Tone; mono?: boolean; dot?: boolean; children: ReactNode; title?: string
}) {
  return (
    <span className={`chip ${tone}${mono ? ' mono' : ''}`} title={title}>
      {dot && <i className="dot" />}
      {children}
    </span>
  )
}

/** 0–1 的权重条。空心轮廓读不出量，所以一律实心填充。 */
export function MeterRow({ label, value, tone, suffix, note, columns, className, skipped, gate, notStartedLabel }: {
  label: ReactNode; value: number; tone?: 'primary' | 'success' | 'caution' | 'danger' | 'info'
  suffix?: ReactNode; note?: ReactNode; columns?: string; className?: string
  /** 这一路根本没发起（fast_path 下的 embedding/llm）。和「发起了、得 0 分」
   *  是两件事：后端在任何网络调用之前就返回了，那两个 0.0 是占位符不是测量值。 */
  skipped?: boolean
  /** 「未发起」的文案由调用方传入：ui.tsx 是纯展示层，不接 i18n context。 */
  notStartedLabel?: string
  /** 协作门槛（0–1）。画在轨道上，省得读者心算差多远。 */
  gate?: number
}) {
  const width = `${Math.max(0, Math.min(1, value)) * 100}%`
  return (
    <div className={`meter-row${className ? ` ${className}` : ''}${skipped ? ' skipped' : ''}`}
         style={columns ? { gridTemplateColumns: columns } : undefined}>
      {/* 主/辅圆点是装饰，独立成元素并对读屏隐藏，别混进标签自己的文本节点 */}
      <span className="k">
        <span>{label}</span>
        {suffix ? <span aria-hidden="true">{suffix}</span> : null}
      </span>
      <span className={`meter${tone ? ` ${tone}` : ''}${gate !== undefined ? ' gated' : ''}`}>
        <i style={{ width }} />
        {gate !== undefined && (
          <b className="gate" style={{ left: `${Math.max(0, Math.min(1, gate)) * 100}%` }} />
        )}
      </span>
      <span className="v">{skipped ? notStartedLabel ?? 'not started' : value.toFixed(2)}</span>
      {note !== undefined && <span className="v" style={{ color: 'var(--color-text-tertiary)' }}>{note}</span>}
    </div>
  )
}

export function Card({ title, badge, actions, children, bodyStyle }: {
  title?: ReactNode; badge?: ReactNode; actions?: ReactNode
  children: ReactNode; bodyStyle?: React.CSSProperties
}) {
  return (
    <section className="card">
      {title && (
        <header className="card-head">
          <h2>{title}</h2>
          {badge}
          <span className="spacer" />
          {actions}
        </header>
      )}
      <div className="card-body" style={bodyStyle}>{children}</div>
    </section>
  )
}

export function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
      {hint && <div className="hint" title={hint}>{hint}</div>}
    </div>
  )
}

export function KV({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <div className="kv"><dt>{label}</dt><dd>{children}</dd></div>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  return <p className="note danger">{error instanceof Error ? error.message : String(error)}</p>
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return <p className="section-label">{children}</p>
}
