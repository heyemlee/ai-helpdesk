/**
 * 统计图的几种画法：占比条、取值清单、按月的柱子、填充率（移植自旧控制台的 charts.js）。
 *
 * 几条规矩，决定了下面每个组件长什么样：
 * - 颜色只干一件事：分类色表示"是哪一个取值"（lib/sql/charts.ts 的 seriesSlots），取值太多就不上色，
 *   改成单色的条形。配色在 data.css，这里只写 viz-sN 这样的类名。
 * - 颜色从不单独出现：每个色块旁边都有名字和数字，那份清单本身就是这张图的数据表。
 * - 文字一律按文本渲染：取值来自业务库，不当 HTML。
 */
import { useT } from '@/i18n'
import type { CellValue } from '@/api/sql/types'
import type { Bar } from '@/lib/sql/charts'
import { formatCount, formatShare } from '@/lib/sql/format'

export interface VizItem {
  value: CellValue
  text: string
  count: number
  /** 第几个分类色（1 起），0 是不上色 */
  slot: number
  active?: boolean
  /** 截过的取值已经不是库里的原值，只显示、不让点 */
  truncated?: boolean
  muted?: boolean
}

/** 占比条。只是看个大概，读数在旁边的清单里 */
export function StackedBar({ items, label }: { items: VizItem[]; label: string }) {
  const { lang } = useT()
  const total = items.reduce((sum, item) => sum + item.count, 0)
  if (!total) return <div className="viz-stack is-empty" role="img" aria-label={label} />
  const summary = items.filter((item) => item.count > 0).map((item) => `${item.text} ${formatCount(item.count, lang)}`).join(' · ')
  return (
    <div className="viz-stack" role="img" aria-label={`${label}: ${summary}`}>
      {items.filter((item) => item.count > 0).map((item) => (
        <span key={String(item.value)} className={`viz-seg viz-s${item.slot}${item.muted ? ' is-muted' : ''}`}
              style={{ flexGrow: item.count }}
              title={`${item.text} · ${formatCount(item.count, lang)} · ${formatShare(item.count, total, lang)}`} />
      ))}
    </div>
  )
}

/**
 * 取值清单：一行一个取值，色点（或条形）+ 名字 + 数量 + 占比。给了 onPick 就是可点的——点一下按这个
 * 取值筛选，再点一下取消。bars：每行画一条和数量成正比的单色条形（取值多、不上色的字段用这个）。
 */
export function ValueList({ items, total, onPick, bars = false }: {
  items: VizItem[]
  total: number
  onPick?: (item: VizItem) => void
  bars?: boolean
}) {
  const { lang } = useT()
  const peak = Math.max(1, ...items.map((item) => item.count))
  const anyActive = items.some((item) => item.active)
  return (
    <ul className={`viz-list${bars ? ' has-bars' : ''}`}>
      {items.map((item) => {
        const share = formatShare(item.count, total, lang)
        const title = `${item.text} · ${formatCount(item.count, lang)}${share ? ` · ${share}` : ''}`
        const body = (
          <>
            {!bars && <span className={`viz-dot${item.slot ? ` viz-s${item.slot}` : ''}`} aria-hidden="true" />}
            <span className="viz-name">{item.text}</span>
            {bars && (
              <span className="viz-track" aria-hidden="true">
                <i style={{ width: `${Math.max(item.count ? 2 : 0, (item.count / peak) * 100)}%` }} />
              </span>
            )}
            <span className="viz-count">{formatCount(item.count, lang)}</span>
            <span className="viz-share">{share}</span>
          </>
        )
        return (
          <li key={String(item.value)}>
            {!onPick || item.truncated
              ? <span className="viz-row is-static" title={title}>{body}</span>
              : (
                <button type="button" className={`viz-row${anyActive && !item.active ? ' is-off' : ''}`}
                        aria-pressed={!!item.active} title={title} onClick={() => onPick(item)}>
                  {body}
                </button>
              )}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * 按月（季、年）的柱子。给了 onPick 就是可点的：点一根柱子按它的日期范围筛选。
 * selected：当前按这个字段筛着的范围，范围外的柱子退后。
 */
export function ColumnChart({ bars, label, onPick, selected = null }: {
  bars: Bar[]
  label: string
  onPick?: (bar: Bar) => void
  selected?: { start: string; end: string } | null
}) {
  const { t, lang } = useT()
  const peak = Math.max(1, ...bars.map((bar) => bar.count))
  const inside = (bar: Bar) => !!selected && bar.start >= selected.start && bar.end <= selected.end
  return (
    <>
      <div className={`viz-cols${onPick ? '' : ' is-static'}`} role={onPick ? 'group' : 'img'} aria-label={label}>
        {bars.map((bar, index) => {
          const height = bar.count ? Math.max(3, (bar.count / peak) * 100) : 0
          const tip = `${bar.label} · ${formatCount(bar.count, lang)}`
          const edge = index < bars.length / 4 ? ' tip-start' : index >= (bars.length * 3) / 4 ? ' tip-end' : ''
          const state = selected ? (inside(bar) ? ' is-on' : ' is-off') : ''
          const mark = <i style={{ height: `${height}%` }} />
          return onPick
            ? (
              <button key={bar.start} type="button" className={`viz-col${state}${edge}`} aria-pressed={inside(bar)}
                      aria-label={t('sq.explore.viz.barLabel', { label: bar.label, n: formatCount(bar.count, lang) })}
                      data-tip={tip} onClick={() => onPick(bar)}>
                {mark}
              </button>
            )
            : <span key={bar.start} className={`viz-col${state}`} title={tip}>{mark}</span>
        })}
      </div>
      <div className="viz-axis" aria-hidden="true">
        <span>{bars[0]?.label ?? ''}</span>
        <span>{bars.length > 1 ? bars[bars.length - 1]!.label : ''}</span>
      </div>
    </>
  )
}

/** 填充率：这个字段有多少条记录填了值 */
export function Meter({ part, whole, label }: { part: number; whole: number; label: string }) {
  const share = whole > 0 ? Math.min(1, Math.max(0, part / whole)) : 0
  return <span className="viz-meter" role="img" aria-label={label}><i style={{ width: `${share * 100}%` }} /></span>
}

/** 一行里和数量成正比的条形（目录页上各张表的记录数） */
export function BarRow({ count, peak }: { count: number; peak: number }) {
  const width = peak > 0 && count > 0 ? Math.max(2, (count / peak) * 100) : 0
  return <span className="viz-track" aria-hidden="true"><i style={{ width: `${width}%` }} /></span>
}
