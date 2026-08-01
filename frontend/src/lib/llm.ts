import type { Key } from '@/i18n/catalog'

/**
 * 模型服务不可用的原因（后端 core/llm_guard 的 REASON_*）→ 文案键。
 * 认不出的（后端以后新增的、老后端没报的）归到"未报告"，不猜它的意思。
 */
const REASON_KEYS: Record<string, Key> = {
  credential_rejected: 'llm.reason.credential_rejected',
  permission_denied: 'llm.reason.permission_denied',
  quota_exhausted: 'llm.reason.quota_exhausted',
  rate_limited: 'llm.reason.rate_limited',
  forbidden: 'llm.reason.forbidden',
  model_error: 'llm.reason.model_error',
}

export const llmReasonKey = (reason: string | null | undefined): Key =>
  REASON_KEYS[reason ?? ''] ?? 'llm.reason.unknown'

/**
 * 接下来会怎样，和后端的处理对齐：
 *   reset —— 凭证被拒、不被允许、额度用尽：等再久也不会自己好，要有人处理；
 *   wait  —— 限流、原因不明的 403：有限退避，到期自己放行；
 *   retry —— 这一次调用没成（超时、上游报错），和凭证无关，再发一次就行。
 */
export function llmRecovery(reason: string | null | undefined): 'reset' | 'wait' | 'retry' {
  if (reason === 'credential_rejected' || reason === 'permission_denied' || reason === 'quota_exhausted') {
    return 'reset'
  }
  return reason === 'rate_limited' || reason === 'forbidden' ? 'wait' : 'retry'
}
