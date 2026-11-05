import { useT } from '@/i18n'
import { href } from '@/lib/route'

/**
 * 从一轮对话跳到 SQL Agent 那边的那次查询（#/data/runs/<run_id>）：每一步的 SQL、模型调用、耗时。
 *
 * run_id 是员工实例委派时记下的（A2A 的 taskId、/query 的 X-Run-Id，写在 business_data 和工具调用的
 * trace 里）。没有就不画：早于这个字段的轮次、委派没发出去的、SQL Agent 没回 run_id 的都是这样。
 */
export function RunLink({ runId }: { runId?: unknown }) {
  const { t } = useT()
  if (typeof runId !== 'string' || !runId) return null
  return <a className="run-link" href={href.dataRun(runId)}>{t('lm.data.openRun')}</a>
}
