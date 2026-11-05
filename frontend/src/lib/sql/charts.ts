/**
 * 统计图背后的两段纯逻辑（移植自旧控制台的 charts.js）：取值配什么色、按月的记录数怎么并成柱子。
 * 画法在 components/data/explore/charts.tsx。
 *
 * 颜色只干一件事：分类色表示"是哪一个取值"，按字段取值的固定顺序分配，筛选前后同一个取值永远是
 * 同一个颜色；取值超过 MAX_SERIES 个就不上色，改成单色的条形——再多的颜色互相分不清。
 */
import type { CellValue } from '@/api/sql/types'

export const MAX_SERIES = 6
const MAX_BARS = 36

export interface FacetLike {
  complete: boolean
  ordered_by: string
  values: Array<{ value: CellValue; count: number }>
}

/**
 * 取值 → 第几个颜色（1 起）。facet 是这个字段**没有筛选时**的完整取值清单，顺序由服务端定
 * （声明过的按声明顺序）。取不全或者太多的不上色，返回空表。
 */
export function seriesSlots(facet: FacetLike | null | undefined): Map<string, number> {
  const slots = new Map<string, number>()
  if (!facet || !facet.complete || facet.values.length > MAX_SERIES) return slots
  // 没声明顺序的按取值本身排，不按数量排：数量会变，颜色不能跟着换
  const values = facet.ordered_by === 'declared'
    ? facet.values
    : [...facet.values].sort((a, b) => String(a.value).localeCompare(String(b.value), undefined, { numeric: true }))
  values.forEach((item, index) => slots.set(String(item.value), index + 1))
  return slots
}

export interface Bar {
  label: string
  /** 这根柱子覆盖的日期范围 [start, end)，点它的时候就是拿这两个日期去筛选 */
  start: string
  end: string
  count: number
}

export type Grain = 'month' | 'quarter' | 'year'

const pad = (number: number) => String(number).padStart(2, '0')
const monthIndex = (period: string) => Number(period.slice(0, 4)) * 12 + Number(period.slice(5, 7)) - 1
const monthStart = (index: number) => `${Math.floor(index / 12)}-${pad((index % 12) + 1)}-01`

/**
 * 按月的记录数 → 画得下的柱子。
 *
 * 月份之间的空档补成 0（没有记录的月份也是一个月，不能把两头直接拼在一起）；月份太多时并成季度，
 * 还多就并成年。季度和年按日历对齐（一季度从一月起），不从数据的第一个月起算。
 */
export function rebucket(buckets: Array<{ period: string; count: number }> | null | undefined): { grain: Grain; bars: Bar[] } {
  const counts = new Map<number, number>()
  for (const bucket of buckets ?? []) {
    if (/^\d{4}-(0[1-9]|1[0-2])$/.test(bucket.period)) counts.set(monthIndex(bucket.period), bucket.count)
  }
  if (!counts.size) return { grain: 'month', bars: [] }

  const first = Math.min(...counts.keys())
  const last = Math.max(...counts.keys())
  const months = last - first + 1
  const span = months <= MAX_BARS ? 1 : Math.ceil(months / 3) <= MAX_BARS + 4 ? 3 : 12
  const grain: Grain = span === 1 ? 'month' : span === 3 ? 'quarter' : 'year'

  const bars: Bar[] = []
  for (let start = first - (first % span); start <= last; start += span) {
    let count = 0
    for (let index = start; index < start + span; index += 1) count += counts.get(index) ?? 0
    const year = Math.floor(start / 12)
    const label = span === 1 ? `${year}-${pad((start % 12) + 1)}`
      : span === 3 ? `${year} Q${Math.floor((start % 12) / 3) + 1}` : String(year)
    bars.push({ label, start: monthStart(start), end: monthStart(start + span), count })
  }
  return { grain, bars }
}

/**
 * [start, end) 正好是一个月、一个季度或一年时，写成人读的样子（2024-03、2024 Q2、2024）；
 * 否则返回 null，由调用方写起止日期。
 */
export function periodName(range: { start: string; end: string }): string | null {
  const match = /^(\d{4})-(\d{2})-01$/
  const a = match.exec(range.start)
  const b = match.exec(range.end)
  if (!a || !b) return null
  const months = (Number(b[1]) - Number(a[1])) * 12 + Number(b[2]) - Number(a[2])
  const month = Number(a[2])
  if (months === 1) return `${a[1]}-${a[2]}`
  if (months === 3 && month % 3 === 1) return `${a[1]} Q${(month + 2) / 3}`
  if (months === 12 && month === 1) return a[1]!
  return null
}
