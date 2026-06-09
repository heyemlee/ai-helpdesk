import { useRunEval } from '@/api/hooks'
import { Badge, EmptyHint, ErrorNote, Panel, ScoreBar } from './ui'
import { num, pct } from '@/lib/format'

const DIMENSIONS = [
  { key: 'relevance', label: '相关性' },
  { key: 'accuracy', label: '准确性' },
  { key: 'completeness', label: '完整性' },
  { key: 'helpfulness', label: '有用性' },
] as const

/**
 * 评测面板：一键跑 LLM-as-judge 四维评分，并与存档基线做回归对比。
 *
 * 关键是 regressions 那一栏——基线不会每次跑完自动覆盖，
 * 只有显式 promote 才更新，所以"相比基线退化"是真的在对比历史，
 * 而不是在跟上一次运行比。
 */
export function EvalPanel({ base }: { base: string }) {
  const { mutate, data, error, isPending } = useRunEval(base)

  return (
    <Panel
      title="端到端评测"
      badge={<Badge tone="info">LLM-as-Judge</Badge>}
      actions={
        <button
          onClick={() => mutate()}
          disabled={isPending}
          className="rounded-lg bg-sky-500/15 px-2.5 py-1 text-[11px] font-medium text-sky-300
                     ring-1 ring-inset ring-sky-500/25 transition hover:bg-sky-500/25
                     disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isPending ? '评测中…' : '运行评测'}
        </button>
      }
    >
      <div className="space-y-3">
        <ErrorNote error={error} />
        {isPending && <EmptyHint>正在跑用例并让 LLM 打分，通常需要十几秒…</EmptyHint>}
        {!data && !isPending && !error && (
          <EmptyHint>点击「运行评测」跑内置用例，四个维度打分并与基线对比</EmptyHint>
        )}

        {data && (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={data.pass_rate >= 0.8 ? 'good' : data.pass_rate >= 0.6 ? 'warn' : 'bad'}>
                通过率 {pct(data.pass_rate, 1)}
              </Badge>
              <Badge tone="muted">{data.passed}/{data.total} 用例</Badge>
              {data.release && <Badge tone="info">{data.release}</Badge>}
              {data.promoted && <Badge tone="good">已提升为新基线</Badge>}
            </div>

            <div>
              <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
                四维质量分
              </h3>
              <dl className="space-y-1">
                {DIMENSIONS.map(({ key, label }) => {
                  const v = data.avg_scores[key]
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <dt className="w-16 text-[11px] text-slate-400">{label}</dt>
                      <dd className="flex-1">
                        <ScoreBar
                          value={v ?? 0}
                          tone={(v ?? 0) >= 0.75 ? 'good' : (v ?? 0) >= 0.6 ? 'warn' : 'bad'}
                          label={num(v)}
                        />
                      </dd>
                    </div>
                  )
                })}
                {data.avg_scores.intent_accuracy !== undefined && (
                  <div className="flex items-center gap-2 border-t border-slate-800 pt-1.5">
                    <dt className="w-16 text-[11px] text-slate-400">意图准确</dt>
                    <dd className="flex-1">
                      <ScoreBar
                        value={data.avg_scores.intent_accuracy}
                        tone={data.avg_scores.intent_accuracy >= 0.9 ? 'good' : 'warn'}
                        label={num(data.avg_scores.intent_accuracy)}
                      />
                    </dd>
                  </div>
                )}
              </dl>
            </div>

            <div>
              <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
                回归检测
                {data.baseline.release && (
                  <span className="ml-1 font-normal normal-case text-slate-600">
                    vs 基线 {data.baseline.release}
                  </span>
                )}
              </h3>
              {data.regressions.length === 0 ? (
                <p className="rounded-lg bg-emerald-500/8 px-2.5 py-1.5 text-[11px] text-emerald-300">
                  相比基线没有退化
                </p>
              ) : (
                <ul className="space-y-1">
                  {data.regressions.map((r, i) => (
                    <li key={i} className="rounded-lg bg-rose-500/8 px-2.5 py-1.5 text-[11px] text-rose-300">
                      {r}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {data.recommendations.length > 0 && (
              <details className="text-[11px]">
                <summary className="cursor-pointer text-slate-500 hover:text-slate-300">
                  优化建议 · {data.recommendations.length}
                </summary>
                <ul className="mt-1.5 space-y-1">
                  {data.recommendations.map((r, i) => (
                    <li key={i} className="rounded-lg bg-slate-950/60 px-2.5 py-1.5 leading-relaxed text-slate-400">
                      {r}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>
    </Panel>
  )
}
