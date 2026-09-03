import { useEffect, useState } from 'react'
import { useRunEval } from '@/api/hooks'
import { Card, Chip, Empty, ErrorNote } from './ui'
import { num, pct } from '@/lib/format'
import { useT } from '@/i18n'

const DIMENSIONS = [
  { key: 'relevance', label: 'ev.dim.relevance' },
  { key: 'accuracy', label: 'ev.dim.accuracy' },
  { key: 'completeness', label: 'ev.dim.completeness' },
  { key: 'helpfulness', label: 'ev.dim.helpfulness' },
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
  const { t } = useT()
  const { run, data, error, isPending, startedAt } = useRunEval(base)
  const [onlyFailed, setOnlyFailed] = useState(false)

  const results = (data?.results ?? []).filter((r) => !onlyFailed || !r.passed)

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="page-title">{t('ev.pageTitle')}</h2>
            <p className="page-lede">
              {t('ev.lede1')} <code className="mono">/chat</code>{t('ev.lede2')}
            </p>
          </div>
          <button className="btn btn-primary" onClick={run}
                  disabled={isPending} aria-busy={isPending}>
            {isPending ? t('ev.running.btn') : data ? t('ev.rerun') : t('ev.run')}
          </button>
        </div>

        <ErrorNote error={error} />

        {isPending && (
          <Card>
            <div className="empty">
              <span className="thinking" style={{ justifyContent: 'center' }}>
                <span className="pulse" />{t('ev.running')}
                {startedAt !== undefined && (
                  <span className="mono" style={{ marginLeft: 6 }}><Elapsed since={startedAt} /></span>
                )}
              </span>
              <p style={{ marginTop: 10, fontSize: 'var(--text-xs)' }}>
                {t('ev.runningHint')}
              </p>
            </div>
          </Card>
        )}

        {!data && !isPending && !error && (
          <Card>
            <Empty>{t('ev.empty')}</Empty>
          </Card>
        )}

        {data && !isPending && (
          <>
            <div className="chip-row">
              <Chip tone={data.pass_rate >= 0.8 ? 'success' : data.pass_rate >= 0.6 ? 'caution' : 'danger'}>
                {t('ev.passRate')} {pct(data.pass_rate, 1)}
              </Chip>
              <Chip>{data.passed}/{data.total} {t('ev.casesUnit')}</Chip>
              {data.release && <Chip tone="info" mono>{data.release}</Chip>}
              {data.promoted && <Chip tone="success">{t('ev.promoted')}</Chip>}
            </div>

            <div style={{ display: 'grid', gap: 20, gridTemplateColumns: 'repeat(auto-fit,minmax(min(320px,100%),1fr))' }}>
              <Card title={t('ev.axes')} actions={<Chip>0–1</Chip>}>
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
                        <span className="k">{t('ev.dim.intent')}</span>
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
                title={t('ev.regress')}
                actions={data.baseline?.release && (
                  <span className="topbar-sub mono">{t('ev.vsBaseline')} {data.baseline.release}</span>
                )}
              >
                {data.regressions.length === 0
                  ? <p className="note success">{t('ev.noRegress')}</p>
                  : <div className="dl">{data.regressions.map((r, i) => <p className="note danger" key={i}>{r}</p>)}</div>}
              </Card>
            </div>

            {data.results.length > 0 && (
              <Card
                title={t('ev.cases')}
                actions={
                  <div className="segmented" role="tablist" aria-label={t('ev.caseFilter')}>
                    <button role="tab" aria-selected={!onlyFailed} onClick={() => setOnlyFailed(false)}>{t('ev.all')}</button>
                    <button role="tab" aria-selected={onlyFailed} onClick={() => setOnlyFailed(true)}>{t('ev.failedOnly')}</button>
                  </div>
                }
                bodyStyle={{ padding: '8px 12px 12px' }}
              >
                <div className="dl" style={{ gap: 4 }}>
                  {results.map((r) => (
                    <details className="raw" key={r.test_id} style={{ padding: '8px 10px', borderRadius: 'var(--radius-lg)' }}>
                      <summary style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-text)' }}>
                        <Chip tone={r.passed ? 'success' : 'danger'} dot>{r.passed ? t('ev.pass') : t('ev.fail')}</Chip>
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
              <Card title={t('ev.sugg')}>
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
