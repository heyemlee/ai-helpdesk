/** 展示层的小工具。集中放这里，避免各组件各写一套格式化。 */

export const pct = (v: number | undefined, digits = 0): string =>
  v === undefined ? '-' : `${(v * 100).toFixed(digits)}%`

export const ms = (v: number | undefined): string =>
  v === undefined ? '-' : v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`

export const num = (v: number | undefined, digits = 2): string =>
  v === undefined ? '-' : v.toFixed(digits)

/** 置信度配色：越低越危险。阈值和后端的转人工门槛对齐。 */
export function confidenceTone(v: number): 'good' | 'warn' | 'bad' {
  if (v >= 0.7) return 'good'
  if (v >= 0.45) return 'warn'
  return 'bad'
}

/** 熔断器状态配色 */
export function circuitTone(state: string): 'good' | 'warn' | 'bad' {
  if (state === 'closed') return 'good'
  if (state === 'half_open') return 'warn'
  return 'bad'
}

/** 徽章里用的短名。完整的对外角色名由后端 routing.display_names 提供。 */
export const AGENT_LABELS: Record<string, string> = {
  general: '通用',
  technical: '技术',
  billing: '账单',
  order: '订单',
  handoff: '转人工',
}

export const agentLabel = (v: string): string => AGENT_LABELS[v] ?? v
