/**
 * 服务端给的闭集取值（阶段名、run 状态、入口……）→ 文案键。
 *
 * 这些取值以后可能会多出新的：认识的翻成人话，不认识的原样显示（拆成能读的词），不报错、不留白。
 */
import { CATALOG, type Key } from '@/i18n/catalog'
import { humanize } from './format'

export const hasKey = (key: string): key is Key => key in CATALOG.en

type T = (key: Key, vars?: Record<string, string | number>) => string

/** 一个阶段的名字。契约的（generation）和服务端内部的（sql_generation）都认 */
export function stageLabel(stage: string, t: T): string {
  const key = `sq.stage.${stage}`
  return hasKey(key) ? t(key) : humanize(stage)
}

/** prefix 下有这个取值就翻，没有就原样（拆开）显示 */
export function closedSet(prefix: string, value: string, t: T): string {
  const key = `${prefix}.${value}`
  return hasKey(key) ? t(key) : humanize(value)
}
