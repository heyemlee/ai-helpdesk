/**
 * 表格排序（移植自旧控制台的 table.js）。问数的结果表和数据浏览的预览表共用。
 */

const NUMERIC_TEXT = /^-?\d+(\.\d+)?$/
const INTEGER_TEXT = /^-?\d+$/

/**
 * 两个值按数字比的结果；有一个不是数字就返回 null。
 *
 * 数字不一定是 number 类型：PostgreSQL 的 NUMERIC、超过 2^53 的整数到页面上都是字符串（服务端特意
 * 这样给，免得精度丢了）。把它们按字符比的话，-0.50、-9.00、-10.00 就是"升序"。
 * 超出双精度的长整数用 BigInt 比，不经过 Number。
 */
export function compareNumeric(left: unknown, right: unknown): number | null {
  const numeric = (value: unknown) => typeof value === 'number' || (typeof value === 'string' && NUMERIC_TEXT.test(value))
  if (!numeric(left) || !numeric(right)) return null
  const long = (value: unknown) => typeof value === 'string' && INTEGER_TEXT.test(value) && value.length > 15
  if (long(left) || long(right)) {
    try {
      const big = (value: unknown) => BigInt(typeof value === 'number' ? Math.trunc(value) : String(value).split('.')[0]!)
      const a = big(left)
      const b = big(right)
      if (a !== b) return a < b ? -1 : 1
    } catch { /* 落到下面按 Number 比 */ }
  }
  return Number(left) - Number(right)
}

const isEmpty = (value: unknown) => value === null || value === undefined || value === ''

/**
 * 排序只动显示顺序。空值永远排在最后，升序降序都一样——"没有值"不是"最小的值"。
 */
export function sortRows<R extends unknown[]>(rows: R[], index: number, direction: 'asc' | 'desc'): R[] {
  const factor = direction === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const left = a[index]
    const right = b[index]
    if (isEmpty(left) || isEmpty(right)) return Number(isEmpty(left)) - Number(isEmpty(right))
    const numeric = compareNumeric(left, right)
    if (numeric !== null) return numeric * factor
    return String(left).localeCompare(String(right), undefined, { numeric: true }) * factor
  })
}
