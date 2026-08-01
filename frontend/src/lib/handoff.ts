import type { Key } from '@/i18n/catalog'

type Translate = (key: Key, vars?: Record<string, string | number>) => string

/**
 * 后端记下的转人工原因是一组固定句式的中文（agent_orchestrator 的
 * `_handoff_signals`、`_route_decision` 和流式路径上那几句），多条用"；"连着，
 * 原样存在工单的 reason 里。
 *
 * 中文界面原样显示。英文界面逐条对着句式译出来，数字照抄——只换语言，不改写、
 * 不概括；认不出的句式（后端以后新增的）原样留着，不猜它的意思。
 */
const PATTERNS: Array<[RegExp, Key]> = [
  [/^专家 Agent 与通用 Agent 均执行失败$/, 'ho.bothFailed'],
  [/^没有可用的 Agent 实例$/, 'ho.noAgent'],
  [/^Agent 流式执行失败$/, 'ho.streamFailed'],
  [/^意图置信度 (?<value>[\d.]+) 低于 (?<min>[\d.]+)$/, 'ho.intent'],
  [/^路由领域分 (?<value>[\d.]+) 低于 (?<min>[\d.]+)$/, 'ho.routing'],
  [/^Agent 自评置信度 (?<value>[\d.]+) 低于 (?<min>[\d.]+)，其中答案策略拦截 (?<n>\d+) 处无依据承诺$/, 'ho.selfCut'],
  [/^Agent 自评置信度 (?<value>[\d.]+) 低于 (?<min>[\d.]+)$/, 'ho.self'],
  [/^紧急度为 CRITICAL，直接转人工$/, 'ho.critical'],
  [/^意图为 (?<intent>[\w-]+)，用户明确要求人工$/, 'ho.asked'],
]

export function handoffReason(reason: string | null | undefined, t: Translate, lang: string): string {
  const text = (reason ?? '').trim()
  if (!text || lang !== 'en') return text
  return text.split('；').map((raw) => {
    const clause = raw.trim()
    for (const [pattern, key] of PATTERNS) {
      const match = pattern.exec(clause)
      if (!match) continue
      const { n, ...vars } = match.groups ?? {}
      return t(key, n === undefined ? vars : { ...vars, n: Number(n) })
    }
    return clause
  }).filter(Boolean).join(' ')
}
