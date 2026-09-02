import { useEffect, useState } from 'react'
import { useRunEval } from '@/api/hooks'
import { Card, Chip, Empty, ErrorNote } from './ui'
import { num, pct } from '@/lib/format'

const DIMENSIONS = [
  { key: 'relevance', label: '相关性' },
  { key: 'accuracy', label: '准确性' },
  { key: 'completeness', label: '完整性' },
  { key: 'helpfulness', label: '有用性' },
] as const

const tone = (v: number) => (v >= 0.75 ? 'success' : v >= 0.6 ? 'caution' : 'danger')

/**
 * 已运行时长。评测要跑好几分钟，静态的"评测中…"和真的卡死长得一模一样，
 * 这个计时器是页面上唯一能证明"它还活着"的东西。
 */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [since])
  const total = Math.max(0, Math.round((now - since) / 1000))
  return <>{`${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`}</>
}

/**
 * 端到端评测：一键跑 LLM-as-judge 四维评分，并与存档基线做回归对比。
 *
 * 关键是 regressions 那一栏——基线不会每次跑完自动覆盖，只有显式 promote 才更新，
 * 所以"相比基线退化"是真的在对比历史，而不是在跟上一次运行比。
 */
export function EvalPanel({ base }: { base: string }) {
  const { run, data, error, isPending, startedAt } = useRunEval(base)
  const [onlyFailed, setOnlyFailed] = useState(false)

  const results = (data?.results ?? []).filter((r) => !onlyFailed || !r.passed)

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="page-title">LLM-as-Judge 四维评分</h2>
            <p className="page-lede">
              评测走的是和 <code className="mono">/chat</code> 完全同一套检索与编排，测出来的分数就是线上表现。
              基线不会跑完自动覆盖——只有显式 promote 才更新，所以「相比基线退化」比的是历史，不是上一次运行。
            </p>
          </div>
          <button className="btn btn-primary" onClick={run}
                  disabled={isPending} aria-busy={isPending}>
            {isPending ? '评测中…' : data ? '重新运行' : '运行评测'}
          </button>
        </div>

        <ErrorNote error={error} />

        {isPending && (
          <Card>
            <div className="empty">
              <span className="thinking" style={{ justifyContent: 'center' }}>
                <span className="pulse" />正在跑用例并让 LLM 打分…
                {startedAt !== undefined && (
                  <span className="mono" style={{ marginLeft: 6 }}><Elapsed since={startedAt} /></span>
                )}
              </span>
              <p style={{ marginTop: 10, fontSize: 'var(--text-xs)' }}>
                每条用例都要真的走一遍检索、编排和 LLM 打分，整轮通常要几分钟。
                中途切到别的页面不会中断，回来还在这里。
              </p>
            </div>
          </Card>
        )}

        {!data && !isPending && !error && (
          <Card>
            <Empty>点击右上角「运行评测」跑内置用例，四个维度打分并与存档基线对比。</Empty>
          </Card>
        )}

        {data && !isPending && (
          <>
            <div className="chip-row">
              <Chip tone={data.pass_rate >= 0.8 ? 'success' : data.pass_rate >= 0.6 ? 'caution' : 'danger'}>
                通过率 {pct(data.pass_rate, 1)}
              </Chip>
              <Chip>{data.passed}/{data.total} 用例</Chip>
              {data.release && <Chip tone="info" mono>{data.release}</Chip>}
              {data.promoted && <Chip tone="success">已提升为新基线</Chip>}
            </div>

            <div style={{ display: 'grid', gap: 20, gridTemplateColumns: 'repeat(auto-fit,minmax(min(320px,100%),1fr))' }}>
              <Card title="四维质量分" actions={<Chip>0–1</Chip>}>
                <dl className="dl">
                  {DIMENSIONS.map(({ key, label }) => {
                    const v = data.avg_scores[key] ?? 0
                    return (
                      <div className="meter-row" key={key} style={{ gridTemplateColumns: '72px 1fr auto' }}>
                        <span className="k">{label}</span>
                        <span className={`meter ${tone(v)}`}><i style={{ width: `${v * 100}%` }} /></span>
                        <span className="v">{num(v)}</span>
                      </div>
                    )
                  })}
                  {data.avg_scores.intent_accuracy !== undefined && (
                    <div style={{ borderTop: '1px solid var(--color-border-subtle)', paddingTop: 10 }}>
                      <div className="meter-row" style={{ gridTemplateColumns: '72px 1fr auto' }}>
                        <span className="k">意图准确</span>
                        <span className={`meter ${tone(data.avg_scores.intent_accuracy)}`}>
                          <i style={{ width: `${data.avg_scores.intent_accuracy * 100}%` }} />
                        </span>
                        <span className="v">{num(data.avg_scores.intent_accuracy)}</span>
                      </div>
                    </div>
                  )}
                </dl>
              </Card>

              <Card
                title="回归检测"
                actions={data.baseline?.release && (
                  <span className="topbar-sub mono">vs 基线 {data.baseline.release}</span>
                )}
              >
                {data.regressions.length === 0
                  ? <p className="note success">相比基线没有退化。</p>
                  : <div className="dl">{data.regressions.map((r, i) => <p className="note danger" key={i}>{r}</p>)}</div>}
              </Card>
            </div>

            {data.results.length > 0 && (
              <Card
                title="用例明细"
                actions={
                  <div className="segmented" role="tablist" aria-label="用例筛选">
                    <button role="tab" aria-selected={!onlyFailed} onClick={() => setOnlyFailed(false)}>全部</button>
                    <button role="tab" aria-selected={onlyFailed} onClick={() => setOnlyFailed(true)}>仅未通过</button>
                  </div>
                }
                bodyStyle={{ padding: '8px 12px 12px' }}
              >
                <div className="dl" style={{ gap: 4 }}>
                  {results.map((r) => (
                    <details className="raw" key={r.test_id} style={{ padding: '8px 10px', borderRadius: 'var(--radius-lg)' }}>
                      <summary style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-text)' }}>
                        <Chip tone={r.passed ? 'success' : 'danger'} dot>{r.passed ? '通过' : '未通过'}</Chip>
                        <span className="mono" style={{ fontSize: 'var(--text-xs)' }}>{r.test_id}</span>
                      </summary>
                      <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
                        <div className="dl" style={{ gap: 5 }}>
                          {DIMENSIONS.map(({ key, label }) => {
                            const v = r.scores[key] ?? 0
                            return (
                              <div className="meter-row" key={key} style={{ gridTemplateColumns: '72px 1fr auto' }}>
                                <span className="k">{label}</span>
                                <span className={`meter ${tone(v)}`}><i style={{ width: `${v * 100}%` }} /></span>
                                <span className="v">{num(v)}</span>
                              </div>
                            )
                          })}
                        </div>
                        <p style={{ fontSize: 'var(--text-xs)', lineHeight: '18px', color: 'var(--color-text-secondary)' }}>
                          {r.detail}
                        </p>
                      </div>
                    </details>
                  ))}
                </div>
              </Card>
            )}

            {data.recommendations.length > 0 && (
              <Card title="优化建议">
                <div className="dl">
                  {data.recommendations.map((r, i) => <p className="note neutral" key={i}>{r}</p>)}
                </div>
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  )
}
