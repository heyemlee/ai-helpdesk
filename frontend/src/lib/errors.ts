import { ApiError, getStaffKey } from '@/api/client'
import type { Key } from '@/i18n/catalog'

type T = (key: Key, vars?: Record<string, string | number>) => string

/**
 * 401 的两种情况后端是同一句话（"缺少或错误的 X-API-Key"），但员工要做的事不一样：
 * 没填的去填，填了的去核对。这一页带没带钥匙前端自己知道，照实分开说。
 */
export const unauthorizedKey = (): Key => (getStaffKey() ? 'err.401.rejected' : 'err.401')

/**
 * 把接口错误翻成"接下来该做什么"。后端的原话放在 detail 里一起给——
 * 它通常是中文的诊断串，对排障有用，但不该是员工看到的第一句话。
 *
 * notFound：这个接口返回 404 时意味着什么。同一个 404，在文档列表上是
 * "后端还是旧版本"，在某一张工单上是"这张工单不存在"，得由调用方说。
 */
export function explainError(error: unknown, t: T, notFound?: Key): { body: string; detail: string } {
  const detail = error instanceof Error ? error.message : String(error ?? '')
  if (error instanceof ApiError) {
    if (error.status === 401) return { body: t(unauthorizedKey()), detail }
    if (error.status === 404 && notFound) return { body: t(notFound), detail }
    if (error.status === 503) return { body: t('err.503'), detail }
    return { body: t('err.server'), detail }
  }
  // fetch 自己抛的（连不上、被中断）没有状态码
  return { body: t('err.network'), detail }
}
