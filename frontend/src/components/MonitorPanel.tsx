import { useState } from 'react'
import { useMonitor } from '@/api/hooks'
import { Card, Chip, Empty, MeterRow, Stat } from './ui'
import { Icon } from './Icon'
import { agentLabel, circuitTone, ms, num, pct } from '@/lib/format'
import type { AgentStat, MonitorSummary } from '@/api/types'

type MonitorAlert = MonitorSummary['active_alerts'][number]

/**
 * 在线监控：Agent 在线表现 → Monitor 惩罚 → 路由权重的闭环。
 *
 * 看点是同一类型的多个实例：某个实例失败率上去之后，monitor_penalty 会把它的
 * routing_score 压下去，流量自动倒给同类型的另一个实例。单实例部署时看不出这个闭环，
 * 所以面板按类型分组显示。
 */
export function MonitorPanel({ base }: { base: string }) {
  const { data, error, isLoading, refetch, isFetching, dataUpdatedAt } = useMonitor(base)
  const [onlyPenalized, setOnlyPenalized] = useState(false)

  // 实例 key 形如 billing_0 / billing_1，去掉序号就是类型，按类型分组才看得出权重差异
  const grouped = Object.entries(data?.agent_stats ?? {})
    .filter(([, s]) => !onlyPenalized || s.monitor_penalty > 0)
    .reduce<Record<string, Array<[string, AgentStat]>>>((acc, entry) => {
      const type = entry[0].replace(/_\d+$/, '')
      ;(acc[type] ??= []).push(entry)
      return acc
    }, {})

  const intent = data?.routing?.intent
  const policy = data?.routing?.answer_policy
  const tools = Object.entries(data?.tool_stats ?? {})
  // 后端 active_alerts 是追加日志：同一个 metric 每轮 Monitor（10s）复发一条，
  // 带各自的 ts。直接按条数展示，会把「4 个条件持续告警」说成「10 条告警」，
  // 列表里还并排出现两条一模一样的。按 metric 收敛成当前状态，保留最新一条。
  const alerts = Object.values(
    (data?.active_alerts ?? []).reduce<Record<string, MonitorAlert>>(
      (acc, a) => { acc[a.metric] = a; return acc }, {},
    ),
  )
  const suggestions = data?.suggestions ?? []

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="page-title">意图通道与出站策略</h2>
            <p className="page-lede">
              规则快通道命中时，这一轮不会发起任何意图识别 LLM 调用；答案策略在回复出站前切除没有依据的价格与交期承诺。
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            {!error && dataUpdatedAt > 0 && (
              <span className="topbar-sub mono">
                更新于 {new Date(dataUpdatedAt).toTimeString().slice(0, 8)}
              </span>
            )}
            <button className="icon-btn" title="刷新" aria-label="刷新"
                    disabled={isFetching} onClick={() => refetch()}>
              <Icon name="refresh" />
            </button>
          </div>
        </div>

        {isLoading && <Empty>加载中…</Empty>}

        {/* 后端取不到数据时，绝不逐张卡片声称"没有告警""暂无建议"——
            那是把"不知道"说成"零"。整页收敛成一个连接失败状态。 */}
        {error && (
          <Card>
            <div className="empty">
              <p style={{ color: 'var(--color-text)' }}>连不上后端，监控数据取不到。</p>
              <p className="note danger" style={{ margin: '10px auto 0', maxWidth: '52ch', textAlign: 'left' }}>
                {error instanceof Error ? error.message : String(error)}
              </p>
              <button className="btn btn-secondary" style={{ marginTop: 14 }}
                      disabled={isFetching} onClick={() => refetch()}>
                {isFetching ? '重试中…' : '重试'}
              </button>
            </div>
          </Card>
        )}

        {intent && (
          <dl className="stat-grid">
            <Stat label="规则通道命中" value={pct(intent.fast_path_rate, 1)}
                  hint={`${intent.fast_path_hits} / ${intent.fast_path_hits + intent.ensemble_hits} 次请求`} />
            <Stat label="省下 LLM 调用" value={String(intent.llm_calls_saved)}
                  hint="规则通道跳过的意图识别往返" />
            <Stat label="细粒度意图" value={String(intent.fine_grained_intents)}
                  hint={`Embedding：${intent.embedding_backend}`} />
            <Stat label="意图缓存命中" value={pct(intent.cache.hit_rate, 1)}
                  hint={`缓存 ${intent.cache.size} 条 · 命中 ${intent.cache.hits} / 未命中 ${intent.cache.misses}`} />
          </dl>
        )}

        {policy && (
          <dl className="stat-grid">
            {/* checked_total 不是回复条数：非流式一次回复算 1，流式是逐句
                enforce()（core/answer_policy.py:360），一条回复能算出几十。
                三轮对话就显示 71，写「出站回复总数」是错的。 */}
            <Stat label="策略校验" value={String(policy.checked_total)} hint="校验次数（流式逐句计）" />
            <Stat label="拦截" value={String(policy.blocked_total)}
                  hint={Object.entries(policy.blocked_by_rule).map(([k, v]) => `${k} ${v}`).join(' · ') || '无'} />
            <Stat label="拦截率" value={pct(policy.block_rate, 1)} hint="拦截数 / 校验次数" />
          </dl>
        )}

        {Object.keys(grouped).length > 0 && (
          <Card
            title="Agent 实例路由权重"
            badge={data?.routing && <Chip>每类 {data.routing.replicas_per_type} 实例</Chip>}
            actions={
              <div className="segmented" role="tablist" aria-label="实例筛选">
                <button role="tab" aria-selected={!onlyPenalized} onClick={() => setOnlyPenalized(false)}>全部实例</button>
                <button role="tab" aria-selected={onlyPenalized} onClick={() => setOnlyPenalized(true)}>仅劣化实例</button>
              </div>
            }
          >
            <p className="note neutral" style={{ marginBottom: 14 }}>
              同类型的多个实例共享流量。某个实例失败率上去之后，Monitor 给它记一笔 <code className="mono">monitor_penalty</code>，
              路由权重被压低，流量自动倒给同类型的另一个实例——单实例部署时看不见这个闭环，所以这里按类型分组。
            </p>
            <div className="dl" style={{ gap: 18 }}>
              {Object.entries(grouped).map(([type, rows]) => {
                const head = rows[0]?.[1]
                return (
                <div key={type}>
                  <div className="chip-row" style={{ marginBottom: 8 }}>
                    <strong style={{ fontSize: 'var(--text-sm)' }}>
                      {data?.routing?.display_names?.[type] ?? agentLabel(type)}
                    </strong>
                    <Chip tone={head?.specialist ? 'info' : 'neutral'}>
                      {head?.specialist ? '专家' : '兜底'}
                    </Chip>
                    {head?.model && <Chip mono>{head.model}</Chip>}
                  </div>
                  <div className="dl" style={{ gap: 6 }}>
                    {rows.map(([key, s]) => (
                      <MeterRow
                        key={key}
                        className="agent-row"
                        label={<span className="mono">{key}</span>}
                        value={s.routing_score}
                        tone={s.monitor_penalty > 0 ? 'danger' : 'primary'}
                        note={
                          <>
                            {s.total} 次 · {pct(s.success_rate, 1)} · {ms(s.avg_ms)}
                            {s.monitor_penalty > 0 && (
                              <Chip tone="danger" title="Monitor 判定该实例劣化，已按此数值降低其路由权重">
                                −{num(s.monitor_penalty)}
                              </Chip>
                            )}
                          </>
                        }
                      />
                    ))}
                  </div>
                </div>
                )
              })}
            </div>
          </Card>
        )}

        {tools.length > 0 && (
          <Card title="工具与熔断器" bodyStyle={{ padding: 0 }}>
            <div className="table-scroll">
            <table className="data">
              <thead>
                <tr><th>工具</th><th>调用</th><th>成功率</th><th>平均延迟</th><th>连续失败</th><th>熔断状态</th></tr>
              </thead>
              <tbody>
                {tools.map(([name, s]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td className="n">{s.total}</td>
                    <td className="n">{pct(s.success_rate, 1)}</td>
                    <td className="n">{ms(s.avg_latency_ms)}</td>
                    <td className="n">{s.consecutive_fails}</td>
                    <td><Chip tone={circuitTone(s.circuit_state)} dot>{s.circuit_state}</Chip></td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </Card>
        )}

        {!error && data && (
        <div style={{ display: 'grid', gap: 20, gridTemplateColumns: 'repeat(auto-fit,minmax(min(340px,100%),1fr))' }}>
          <Card title="活跃告警" badge={<Chip tone={alerts.length ? 'caution' : 'neutral'}>{alerts.length}</Chip>}>
            {alerts.length === 0
              ? <Empty>当前没有活跃告警。</Empty>
              : (
                <div className="dl">
                  {alerts.slice(-6).map((a, i) => (
                    <div key={i} className={`note ${a.severity === 'warning' ? 'caution' : 'neutral'}`}>
                      <span className="mono" style={{ fontSize: 'var(--text-3xs)', opacity: 0.75 }}>
                        {a.metric}
                      </span>
                      <br />{a.message}
                    </div>
                  ))}
                </div>
              )}
          </Card>

          <Card title="优化建议" badge={<Chip>按优先级</Chip>}>
            {suggestions.length === 0
              ? <Empty>暂无建议。</Empty>
              : (
                <div className="dl">
                  {[...suggestions].sort((a, b) => a.priority - b.priority).map((s, i) => (
                    <details className="raw" key={i}
                             style={{ padding: '10px 12px', border: '1px solid var(--color-border-subtle)', borderRadius: 'var(--radius-lg)' }}>
                      <summary style={{ color: 'var(--color-text)', fontSize: 'var(--text-sm)' }}>
                        <Chip mono>P{s.priority}</Chip> {s.title}
                      </summary>
                      <p style={{ marginTop: 8, fontSize: 'var(--text-xs)', lineHeight: '18px', color: 'var(--color-text-secondary)' }}>
                        {s.action}
                      </p>
                    </details>
                  ))}
                </div>
              )}
          </Card>
        </div>
        )}
      </div>
    </div>
  )
}
