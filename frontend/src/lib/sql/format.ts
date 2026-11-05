/**
 * 数据页面里名字、数字、时间怎么显示（移植自 SQL Agent 旧控制台的 format.js）。
 *
 * 两条原则：
 * - 单位只来自服务端（语义层声明的 unit、响应里的 column_units / currency），这里从不按列名猜。
 *   猜对了没人知道，猜错了报出去的就是错币种。
 * - 只改"怎么显示"，不改值：复制出去的表格是原始值，不是加了千分位的字符串。
 *
 * 语言由调用方传进来（useT() 的 lang），这里不碰 React。
 */
import type { Lang } from '@/i18n'

const locale = (lang: Lang) => (lang === 'zh' ? 'zh-CN' : 'en-US')

const hasCjk = (text: unknown) => /[㐀-鿿]/.test(String(text ?? ''))
const isPlainAscii = (text: string) => /^[\x20-\x7e]+$/.test(text) && /[a-z]/i.test(text)

// 字段名里常见的缩写。只展开这种没有歧义的；认不出的原样保留。
const ABBREVIATIONS: Record<string, string> = {
  amt: 'amount', qty: 'quantity', no: 'number', num: 'number', id: 'ID', po: 'PO',
  pct: 'percent', desc: 'description', avg: 'average', cnt: 'count',
}

/** 把技术名拆成能读的词：inv_amt → "Inv amount"，ref_no → "Ref number"。 */
export function humanize(name: string): string {
  const words = String(name || '')
    .replace(/[_\-.]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => ABBREVIATIONS[word.toLowerCase()] ?? word.toLowerCase())
  if (!words.length) return String(name || '')
  words[0] = words[0]!.charAt(0).toUpperCase() + words[0]!.slice(1)
  return words.join(' ')
}

export interface Named {
  name: string
  aliases?: string[]
  label?: string
  label_en?: string
  label_zh?: string
}

/**
 * 给人看的名字：语义层显式写的 label 优先，其次是当前语言的第一个别名，最后才是把技术名拆开。
 * 技术名本身总是另外显示，这里不负责。
 */
export function labelOf(entity: Named | null | undefined, lang: Lang): string {
  if (!entity) return ''
  const explicit = entity[`label_${lang}`] || entity.label
  if (explicit) return explicit
  const aliases = Array.isArray(entity.aliases) ? entity.aliases : []
  if (lang === 'zh') {
    const chinese = aliases.find(hasCjk)
    if (chinese) return chinese
  }
  return humanize(aliases.find(isPlainAscii) || entity.name)
}

/**
 * 数据字典里的一段文字（说明、用途、关联备注）→ { text, original }。
 *
 * 有当前语言的版本（`<key>_en` / `<key>_zh`，数据字典里人写的）就用它；没有就照原文给，
 * 并标明是原文。页面不替数据字典翻译：译错一个口径，比让人看到一段中文危险得多。
 */
export function localized(entity: object | null | undefined, key: string, lang: Lang): { text: string; original: boolean } {
  if (!entity) return { text: '', original: false }
  const record = entity as Record<string, unknown>
  const own = record[`${key}_${lang}`]
  if (typeof own === 'string' && own) return { text: own, original: false }
  const text = typeof record[key] === 'string' ? (record[key] as string) : ''
  return { text, original: Boolean(text) && lang === 'en' && hasCjk(text) }
}

export const descriptionOf = (entity: object | null | undefined, lang: Lang) => localized(entity, 'description', lang)

export const isCurrency = (unit: string | null | undefined) => /^[A-Z]{3}$/.test(unit || '')

const ZERO_LOOKING = /^[-−]?0(?:[.,]0+)?$/

export function formatNumber(value: unknown, lang: Lang, { unit = '', plain = false } = {}): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  let text: string
  if (isCurrency(unit)) {
    text = new Intl.NumberFormat(locale(lang), { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
  } else {
    // 编号、年份这类整数加千分位反而难认（2024 → 2,024），所以小于一万的不加
    const grouped = !plain && Math.abs(value) >= 10000
    text = new Intl.NumberFormat(locale(lang), {
      useGrouping: grouped,
      maximumFractionDigits: Number.isInteger(value) ? 0 : 4,
    }).format(value)
  }
  // 固定小数位会把很小的数抹成 0（0.00001234 → "0"，0.004 USD → "0.00"，-0.00001 → "-0"）。
  // 一个不是零的数不能显示成零：这时改按有效数字写。
  if (value !== 0 && ZERO_LOOKING.test(text)) {
    text = new Intl.NumberFormat(locale(lang), { useGrouping: false, maximumSignificantDigits: 4 }).format(value)
  }
  return text
}

/** "有多少条"。数的是记录，不是编号：一律带千分位（1,200）。 */
export function formatCount(value: unknown, lang: Lang): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat(locale(lang), { maximumFractionDigits: 0 }).format(value)
}

/**
 * 统计卡片上的大数：写短（4,703 万 / 47M），完整的数放在悬停提示和表格里。
 * 一百万以内不缩——卡片放得下，缩了反而丢精度。
 */
export function formatCompact(value: unknown, lang: Lang, { unit = '' } = {}): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (Math.abs(value) < 1_000_000) {
    const digits = isCurrency(unit) ? 2 : Number.isInteger(value) ? 0 : 2
    return new Intl.NumberFormat(locale(lang), {
      minimumFractionDigits: isCurrency(unit) ? 2 : 0, maximumFractionDigits: digits,
    }).format(value)
  }
  return new Intl.NumberFormat(locale(lang), { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

/** 占比。不是零的一份不能显示成 0%。 */
export function formatShare(part: number, whole: number, lang: Lang): string {
  if (!(whole > 0) || !(part >= 0)) return ''
  const share = part / whole
  if (part > 0 && share < 0.001) return '<0.1%'
  return new Intl.NumberFormat(locale(lang), {
    style: 'percent', maximumFractionDigits: share < 0.1 ? 1 : 0,
  }).format(share)
}

export interface CellText {
  text: string
  numeric: boolean
  empty: boolean
}

/** 一个单元格 → { text, numeric, empty }。 */
export function formatCell(value: unknown, lang: Lang, options: { unit?: string; plain?: boolean; kind?: string } = {}): CellText {
  if (value === null || value === undefined) return { text: '—', numeric: false, empty: true }
  if (typeof value === 'number') return { text: formatNumber(value, lang, options), numeric: true, empty: false }
  if (typeof value === 'boolean') return { text: String(value), numeric: false, empty: false }
  if (typeof value === 'object') return { text: JSON.stringify(value), numeric: false, empty: false }
  const text = String(value)
  return { text, numeric: /^-?\d+(\.\d+)?$/.test(text) && options.kind === 'number', empty: text === '' }
}

/** 这一列是不是数字列：非空值全是数字。空列不算。 */
export function isNumericColumn<R>(rows: R[], read: (row: R) => unknown): boolean {
  let seen = false
  for (const row of rows) {
    const value = read(row)
    if (value === null || value === undefined) continue
    if (typeof value !== 'number') return false
    seen = true
  }
  return seen
}

export function formatDateTime(iso: string | null | undefined, lang: Lang): string {
  const date = new Date(iso ?? '')
  if (!iso || Number.isNaN(date.getTime())) return String(iso ?? '')
  return new Intl.DateTimeFormat(locale(lang), { dateStyle: 'medium', timeStyle: 'medium' }).format(date)
}

/**
 * 一个时刻。是今天的只写几点；不是今天的连日期一起写——"读取于 5:12 PM"在第二天还开着的页面上，
 * 说的是昨天的 5:12。
 */
export function formatTime(iso: string | null | undefined, lang: Lang, now: number = Date.now()): string {
  const date = new Date(iso ?? '')
  if (!iso || Number.isNaN(date.getTime())) return String(iso ?? '')
  const today = new Date(now)
  const sameDay = date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth() && date.getDate() === today.getDate()
  return new Intl.DateTimeFormat(locale(lang), sameDay
    ? { timeStyle: 'medium' }
    : { dateStyle: 'medium', timeStyle: 'medium' }).format(date)
}

/**
 * "多久以前"。一周以内说相对时间（5 分钟前、昨天），再早就直接写日期——"23 天前"还得自己
 * 倒着数，不如一个日期好认。
 */
export function formatRelative(iso: string | null | undefined, lang: Lang, now: number = Date.now()): string {
  const date = new Date(iso ?? '')
  if (!iso || Number.isNaN(date.getTime())) return String(iso ?? '')
  const seconds = Math.round((date.getTime() - now) / 1000)
  const ago = Math.abs(seconds)
  const relative = new Intl.RelativeTimeFormat(locale(lang), { numeric: 'auto' })
  if (ago < 45) return relative.format(0, 'second')
  if (ago < 3600) return relative.format(Math.round(seconds / 60), 'minute')
  if (ago < 86400) return relative.format(Math.round(seconds / 3600), 'hour')
  if (ago < 7 * 86400) return relative.format(Math.round(seconds / 86400), 'day')
  return new Intl.DateTimeFormat(locale(lang), { dateStyle: 'medium' }).format(date)
}

export function formatDuration(ms: unknown): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return ''
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

// 日期后面要么结束、要么跟着时刻。只看开头十个字符的话，2024-10-12345 这种单据号会被当成
// 2024 年 10 月 12 日。月和日也得在日历上说得通。
const ISO_DATE = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?:$|[T ]\d{2}:\d{2})/

export interface DateRange {
  column: string
  min: string
  max: string
}

/**
 * 结果里的业务日期：哪些列装的是日期，最早和最晚各是哪天。
 *
 * 这是"这批数据是什么时候的"的唯一可靠来源——读库的时刻说明不了业务数据的日期，
 * 一份 2024 年的历史记录今天去读，读库时刻也是今天。
 */
export function recordDates<R>(columns: string[], rows: R[], read: (row: R, column: string) => unknown): DateRange[] {
  const ranges: DateRange[] = []
  for (const column of columns) {
    let min: string | null = null
    let max: string | null = null
    let dated = true
    let seen = false
    for (const row of rows) {
      const value = read(row, column)
      if (value === null || value === undefined || value === '') continue
      if (typeof value !== 'string' || !ISO_DATE.test(value)) { dated = false; break }
      const day = value.slice(0, 10)
      if (min === null || day < min) min = day
      if (max === null || day > max) max = day
      seen = true
    }
    if (dated && seen && min !== null && max !== null) ranges.push({ column, min, max })
  }
  return ranges.slice(0, 3)
}

export const formatRange = (range: { min: string; max: string }) =>
  (range.min === range.max ? range.min : `${range.min} → ${range.max}`)

/** 复制成制表符分隔：贴进表格软件就是一张表。值用原始值。 */
export function toTsv(headers: string[], rows: unknown[][]): string {
  const clean = (value: unknown) => {
    if (value === null || value === undefined) return ''
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
    return text.replace(/[\t\r\n]+/g, ' ')
  }
  return [headers.map(clean).join('\t'), ...rows.map((row) => row.map(clean).join('\t'))].join('\n')
}
