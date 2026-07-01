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

/* Agent 名与单字记号是**闭集**——取值有限、含义固定，所以能安全地跟着界面语言走。
   自由文本（回复正文、routing.reason）不在此列，那些原样显示。
   这几个函数不再持有字面量，改为接收 t：组件在顶层拿到 t 再传进来，
   避免在模块作用域缓存语言（切语言时不会重渲染，是很难查的 bug）。 */
import type { Key } from '@/i18n/catalog'

type T = (key: Key) => string

/** 徽章里用的短名。完整的对外角色名由后端 routing.display_names 提供。 */
const GROUP_KEYS = ['general', 'technical', 'billing', 'order', 'handoff'] as const

export const agentLabel = (v: string, t: T): string =>
  (GROUP_KEYS as readonly string[]).includes(v) ? t(`group.${v}` as Key) : v

/**
 * 对话流里的身份行用的角色名与单字记号。
 *
 * 名称与 backend/agents/agent_orchestrator.py 的 AGENT_DISPLAY_NAMES 对齐。
 * 后端 /health 与 /monitor 也下发 routing.display_names，但那是**中文单语**的，
 * 拿它当权威会让英文界面漏出中文，所以闭集内一律以这里为准（见 agentDisplayName）。
 * 单字记号纯粹是排版记号，不是新造的品牌资产；英文用首字母。
 */
const AGENT_TYPES = ['general', 'technical', 'billing', 'order'] as const

export const agentIdentity = (type: string | undefined, t: T): { name: string; mark: string } => {
  if (type === 'handoff') return { name: t('agent.handoff'), mark: t('agent.mark.handoff') }
  if (type && (AGENT_TYPES as readonly string[]).includes(type)) {
    return { name: `${t(`agent.${type}` as Key)} Agent`, mark: t(`agent.mark.${type}` as Key) }
  }
  return { name: type ?? t('agent.pending'), mark: '·' }
}

/**
 * Monitor 分组标题用的完整角色名。
 *
 * 闭集内（general/technical/billing/order/handoff）跟随界面语言；
 * 只有后端新增了前端还不认识的 Agent 类型时，才退回它下发的 display_name——
 * 那时显示一个中文名，也好过显示裸的 key。
 */
export const agentDisplayName = (type: string, t: T, backend?: string): string =>
  (AGENT_TYPES as readonly string[]).includes(type) || type === 'handoff'
    ? agentIdentity(type, t).name
    : backend ?? type
