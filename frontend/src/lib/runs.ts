import type { Key } from '@/i18n/catalog'

/**
 * 按积压降级关掉的环节（后端 app/runs/degrade.py 的 off）→ 员工听得懂的说法。
 * 认不出的环节（后端以后新增的）不编，直接略过。
 */
const OFF_KEYS: Record<string, Key> = {
  tool_loop: 'cw.run.off.tool_loop',
  rewrite_rerank: 'cw.run.off.rewrite_rerank',
  order_data: 'cw.run.off.order_data',
  model_calls: 'cw.run.off.model_calls',
}

export const degradeSummary = (off: string[], t: (k: Key) => string): string =>
  off.map((what) => OFF_KEYS[what]).filter((k): k is Key => !!k).map((k) => t(k)).join('；')
