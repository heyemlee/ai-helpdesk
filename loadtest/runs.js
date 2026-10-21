// 排队模式的压测：按每分钟多少个 run 的到达率加压，每个 run 提交之后轮询到终态。
//
// 量的是"系统能接住多少"，不是"一个请求多快"：到达率逐级往上加，看完成的速率在哪里停住、
// 积压怎么涨、降级档位什么时候上来、门口什么时候开始回 429。时间都取服务端记的
// （GET /runs/{id} 里的 created_at / started_at / finished_at），不受轮询间隔影响。
//
//   docker run --rm --network aihd-drill_default -v "$PWD/loadtest:/scripts" -v "$OUT:/results" \
//     -e API=http://api:8000 -e PROFILE=15,30,60,60 grafana/k6:2.3.0 run /scripts/runs.js
//
// 一般用 loadtest/run.sh，它负责把被测的那一套配好（worker 数、令牌桶、降级、假模型的配额）。
import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'

const BASE = __ENV.API || 'http://api:8000'
// 每一级的到达率（每分钟多少个 run），每级持续 STAGE
const PROFILE = (__ENV.PROFILE || '15,30,60,60').split(',').map(Number)
const STAGE = __ENV.STAGE || '1m'
const WAIT_S = Number(__ENV.WAIT_S || 240)
const TERMINAL = new Set(['succeeded', 'failed', 'dead'])

export const options = {
  scenarios: {
    runs: {
      executor: 'ramping-arrival-rate',
      startRate: PROFILE[0],
      timeUnit: '1m',
      preAllocatedVUs: 50,
      maxVUs: 500,
      stages: PROFILE.map((target) => ({ target, duration: STAGE })),
      gracefulStop: `${WAIT_S}s`,
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'max'],
}

const accepted = new Counter('runs_accepted')
const rejected = new Counter('runs_rejected_429')
const finished = new Counter('runs_finished')
const unavailable = new Counter('runs_answered_unavailable')
const timedOut = new Counter('runs_timed_out')
const retried = new Counter('runs_retried')
const queueWait = new Trend('run_queue_wait_s')
const total = new Trend('run_total_s')
const level = new Trend('run_degrade_level')

const MESSAGES = [
  '补件什么时候能到', '柜门关不严怎么办', '台面上有一道裂缝，需要怎么处理', '安装师傅什么时候上门',
  '报价单里有一项看不懂', '抽屉滑轨有异响', '订单进度怎么样了', '吊柜的层板能不能加一块',
]

export default function () {
  const i = exec.scenario.iterationInTest
  // 拼一个唯一柜号：绕开意图的进程内缓存，每一条都走完整的识别（同 scripts/load_test.py）
  const body = JSON.stringify({
    message: `${MESSAGES[i % MESSAGES.length]}，柜号 W${5000 + i}-${(i % 7) + 1}`,
    user_id: `load-${i % 50}`,
    conv_id: `load-${exec.scenario.startTime}-${i}`,
  })
  const res = http.post(`${BASE}/runs`, body, {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `load-${exec.scenario.startTime}-${i}` },
    tags: { name: 'POST /runs' },
  })
  if (res.status === 429) {
    rejected.add(1)
    return
  }
  if (!check(res, { 'submitted (202)': (r) => r.status === 202 })) return
  accepted.add(1)
  const id = res.json('run_id')
  const t0 = Date.now()
  while ((Date.now() - t0) / 1000 < WAIT_S) {
    sleep(2)
    const r = http.get(`${BASE}/runs/${id}`, { tags: { name: 'GET /runs/{id}' } })
    if (r.status !== 200) continue
    const run = r.json()
    if (!TERMINAL.has(run.status)) continue
    finished.add(1)
    total.add(run.finished_at - run.created_at)
    if (run.started_at) queueWait.add(run.started_at - run.created_at)
    level.add(run.degrade_level || 0)
    if (run.attempt > 1) retried.add(1)
    if (run.result && run.result.service_unavailable) unavailable.add(1)
    return
  }
  timedOut.add(1)
}

export function handleSummary(data) {
  const m = data.metrics
  const v = (name, stat = 'count') => (m[name] && m[name].values[stat]) || 0
  const minutes = (data.state.testRunDurationMs || 0) / 60000
  const lines = [
    `profile ${PROFILE.join(',')} runs/min, ${STAGE} each`,
    `accepted ${v('runs_accepted')}, rejected 429 ${v('runs_rejected_429')}, finished ${v('runs_finished')}, ` +
      `timed out ${v('runs_timed_out')}, answered unavailable ${v('runs_answered_unavailable')}, retried ${v('runs_retried')}`,
    `finished per minute (whole test) ${(v('runs_finished') / (minutes || 1)).toFixed(1)}`,
    `queue wait s: med ${v('run_queue_wait_s', 'med').toFixed(2)} p95 ${v('run_queue_wait_s', 'p(95)').toFixed(2)}`,
    `submit to done s: med ${v('run_total_s', 'med').toFixed(2)} p95 ${v('run_total_s', 'p(95)').toFixed(2)} ` +
      `max ${v('run_total_s', 'max').toFixed(2)}`,
    `degrade level: avg ${v('run_degrade_level', 'avg').toFixed(2)} max ${v('run_degrade_level', 'max')}`,
  ]
  return {
    stdout: lines.join('\n') + '\n',
    [__ENV.SUMMARY || '/results/summary.json']: JSON.stringify(data, null, 2),
  }
}
