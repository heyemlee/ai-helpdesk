import type { StreamStage } from '@/api/sql/client'
import { useT } from '@/i18n'
import { stageProgress } from '@/lib/sql/ask-progress'

/**
 * 等结果时的六段进度。只认服务端真实发来的阶段事件（见 lib/sql/ask-progress.ts）。
 * 进度条本身不念给读屏：当前阶段已经由旁边那句状态文字（role="status"）说了。
 */
export function StageTrack({ stages }: { stages: StreamStage[] }) {
  const { t } = useT()
  return (
    <ol className="sq-stage-track" aria-hidden="true">
      {stageProgress(stages).map((step) => (
        <li key={step.id} className={`sq-stage is-${step.state}`}>{t(`sq.ask.step.${step.id}`)}</li>
      ))}
    </ol>
  )
}
