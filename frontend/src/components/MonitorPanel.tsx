import { useMonitor } from '@/api/hooks'
import { Badge, EmptyHint, ErrorNote, Panel, ScoreBar } from './ui'
import { agentLabel, circuitTone, ms, num, pct } from '@/lib/format'

/**
 * 监控面板：Agent 在线表现 → Monitor 惩罚 → 路由权重的闭环。
 *
 * 看点是同一类型的多个实例：某个实例失败率上去之后，
 * monitor_penalty 会把它的 routing_score 压下去，流量自动倒给同类型的另一个实例。
 * 单实例部署时这个闭环是看不出来的，所以面板按类型分组显示。
 */
export function MonitorPanel({ base }: { base: string }) {
  const { data, error, isLoading } = useMonitor(base)

  const agents = Object.entries(data?.agent_stats ?? {})
  const grouped = agents.reduce<Record<string, typeof agents>>((acc, entry) => {
    const type = entry[0].replace(/_\d+$/, '')
    ;(acc[type] ??= []).push(entry)
    return acc
  }, {})

  const intent = data?.routing?.intent
  const policy = data?.routing?.answer_policy

  return (
    <Panel
      title="在线监控"
      badge={<Badge tone="info">Monitor</Badge>}
      actions={
        data?.routing && (
          <Badge tone="muted">每类 {data.routing.replicas_per_type} 实例</Badge>
        )
      }
    >
      <div className="space-y-4">
        <ErrorNote error={error} />
        {isLoading && <EmptyHint>加载中…</EmptyHint>}

        {/* 意图通道命中率：量化"省掉了多少次 LLM 往返" */}
        {intent && (
          <div className="grid grid-cols-3 gap-2">
            <Stat label="规则通道命中" value={pct(intent.fast_path_rate, 1)}
                  hint={`${intent.fast_path_hits} / ${intent.fast_path_hits + intent.ensemble_hits} 次请求`} />
            <Stat label="省下 LLM 调用" value={String(intent.llm_calls_saved)}
                  hint="规则通道跳过的意图识别往返" />
            <Stat label="细粒度意图" value={String(intent.fine_grained_intents)}
                  hint={`Embedding: ${intent.embedding_backend}`} />
          </div>
        )}

        {/* 答案策略拦截率 */}
        {policy && (
          <div className="grid grid-cols-3 gap-2">
            <Stat label="策略校验" value={String(policy.checked_total)} hint="出站回复总数" />
            <Stat label="拦截" value={String(policy.blocked_total)}
                  hint={Object.entries(policy.blocked_by_rule).map(([k, v]) => `${k}:${v}`).join(' ') || '无'} />
            <Stat label="拦截率" value={pct(policy.block_rate, 1)} hint="无依据承诺占比" />
          </div>
        )}

        {/* Agent 实例：按类型分组，展示路由权重差异 */}
        {Object.keys(grouped).length > 0 && (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
              Agent 实例路由权重
            </h3>
            <div className="space-y-2.5">
              {Object.entries(grouped).map(([type, instances]) => (
                <div key={type}>
                  <div className="mb-1 flex items-center gap-1.5">
                    <span className="text-xs font-medium text-slate-300">
                      {data?.routing?.display_names?.[type] ?? agentLabel(type)}
                    </span>
                    {instances[0]?.[1].specialist
                      ? <Badge tone="info">专家</Badge>
                      : <Badge tone="muted">兜底</Badge>}
                  </div>
                  <div className="space-y-1">
                    {instances.map(([key, stat]) => (
                      <div key={key} className="flex items-center gap-2">
                        <span className="w-20 font-mono text-[10px] text-slate-500">{key}</span>
                        <div className="flex-1">
                          <ScoreBar
                            value={stat.routing_score}
                            tone={stat.monitor_penalty > 0 ? 'bad' : 'good'}
                            label={num(stat.routing_score)}
                          />
                        </div>
                        <span className="w-24 text-right text-[10px] text-slate-500">
                          {stat.total} 次 · {pct(stat.success_rate)} · {ms(stat.avg_ms)}
                        </span>
                        {stat.monitor_penalty > 0 && (
                          <Badge tone="bad" title="Monitor 判定该实例劣化，已降低其路由权重">
                            -{num(stat.monitor_penalty)}
                          </Badge>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 工具与熔断器 */}
        {data?.tool_stats && Object.keys(data.tool_stats).length > 0 && (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
              工具与熔断器
            </h3>
            <div className="space-y-1">
              {Object.entries(data.tool_stats).map(([name, stat]) => (
                <div key={name} className="flex items-center gap-2 text-[11px]">
                  <span className="flex-1 font-mono text-slate-400">{name}</span>
                  <span className="text-slate-500">{stat.total} 次</span>
                  <span className="text-slate-500">{pct(stat.success_rate)}</span>
                  <span className="text-slate-500">{ms(stat.avg_latency_ms)}</span>
                  <Badge tone={circuitTone(stat.circuit_state)}>{stat.circuit_state}</Badge>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 告警 */}
        {data?.active_alerts && data.active_alerts.length > 0 && (
          <div>
            <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
              活跃告警 · {data.active_alerts.length}
            </h3>
            <ul className="space-y-1">
              {data.active_alerts.slice(-4).map((a, i) => (
                <li key={i} className="rounded-lg bg-rose-500/8 px-2.5 py-1.5 text-[11px] text-rose-300">
                  {a.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg bg-slate-950/50 px-2.5 py-2">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className="font-mono text-lg leading-tight text-slate-100">{value}</div>
      {hint && <div className="mt-0.5 truncate text-[10px] text-slate-600" title={hint}>{hint}</div>}
    </div>
  )
}
