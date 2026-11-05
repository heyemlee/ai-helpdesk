import { SqlApiError } from '@/api/sql/client'
import type { Key } from '@/i18n/catalog'

type T = (key: Key, vars?: Record<string, string | number>) => string

/**
 * SQL Agent 的错误 → "接下来该做什么"。服务端的原话（常常是中文的诊断串）放在 detail 里一起给，
 * 对排障有用，但不该是员工看到的第一句话。
 *
 * 401 在这里和工作台别处不一样：浏览器不带 Key，Key 是控制台的 nginx 在服务端加的。
 * 所以 401 不是"你没填 Key"，是服务器上控制台那把 Key 没配或者和 SQL Agent 的对不上——只有管理员能修。
 *
 * notFound：这个接口 404 时意味着什么（某次运行不存在、某张表不存在……），由调用方说。
 */
export function explainSqlError(error: unknown, t: T, notFound?: Key): { body: string; detail: string } {
  if (!(error instanceof SqlApiError)) {
    return { body: t('sq.err.network'), detail: error instanceof Error ? error.message : String(error ?? '') }
  }
  const detail = error.serverMessage || (error.status ? `HTTP ${error.status}` : '')
  if (error.code === 'network' || error.status === 0) return { body: t('sq.err.network'), detail }
  if (error.code === 'bad_response') return { body: t('sq.err.badResponse'), detail }
  if (error.status === 502 || error.status === 504) return { body: t('sq.err.gateway'), detail }
  if (error.status === 401) return { body: t('sq.err.401'), detail }
  if (error.status === 403) return { body: t('sq.err.403'), detail }
  if (error.status === 404 && notFound) return { body: t(notFound), detail }
  if (error.status === 429) return { body: t('sq.err.429'), detail }
  if (error.status === 503) return { body: t('sq.err.503'), detail }
  if (error.status === 400 || error.status === 422) return { body: t('sq.err.invalid'), detail }
  return { body: t('sq.err.server'), detail }
}
