/** 展示层的小工具。集中放这里，避免各组件各写一套格式化。 */

export const pct = (v: number | undefined, digits = 0): string =>
  v === undefined ? '-' : `${(v * 100).toFixed(digits)}%`

export const ms = (v: number | undefined): string =>
  v === undefined ? '-' : v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`

export const num = (v: number | undefined, digits = 2): string =>
  v === undefined ? '-' : v.toFixed(digits)

/** 置信度配色：越低越危险。阈值和后端的转人工门槛对齐。 */
export function confidenceTone(v: number): 'success' | 'caution' | 'danger' {
  if (v >= 0.7) return 'success'
  if (v >= 0.45) return 'caution'
  return 'danger'
}

/** 熔断器状态配色 */
export function circuitTone(state: string): 'success' | 'caution' | 'danger' {
  if (state === 'closed') return 'success'
  if (state === 'half_open') return 'caution'
  return 'danger'
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

/**
 * 对话流里的身份行用的角色名与单字记号。
 *
 * 名称与 backend/agents/agent_orchestrator.py 的 AGENT_DISPLAY_NAMES 保持一致；
 * 后端 /health 与 /monitor 也会下发 routing.display_names，能拿到就以后端为准，
 * 这里只是拿不到时的兜底（对话流不单独为了一个名字去打 /health）。
 * 单字记号纯粹是排版记号，不是新造的品牌资产。
 */
export const AGENT_META: Record<string, { name: string; mark: string }> = {
  general:   { name: '展厅接待 Agent',   mark: '展' },
  technical: { name: '产品与安装 Agent', mark: '产' },
  billing:   { name: '报价与账款 Agent', mark: '报' },
  order:     { name: '订单履约 Agent',   mark: '订' },
}

export const HANDOFF_AGENT = { name: '运营升级通道', mark: '升' }

export const agentIdentity = (type: string | undefined) =>
  (type && AGENT_META[type]) || { name: type ?? '编排中', mark: '·' }
