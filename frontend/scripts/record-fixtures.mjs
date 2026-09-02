#!/usr/bin/env node
/**
 * 从真后端录制测试夹具。
 *
 * 为什么要录而不是手写：手写的 meta/routing/monitor 对象永远长不成真后端的样子——
 * 空数组的实体键、一行不换行的 routing_reason、每 10s 复发一条的告警日志、带负分的
 * 检索结果，这些形状全是接了真后端才现形的，而手写夹具一个都没有。录下来的文件
 * 就是当时的线上形状；后端字段一变，重录一次，前端测试自动知道。
 *
 * 用法（后端要先起着，走 backend/.env 里的网关）：
 *   npm run fixtures:record
 *   VITE_API_URL=http://127.0.0.1:8080 node scripts/record-fixtures.mjs
 *
 * 产物在 src/test/fixtures/，manifest.json 记录录制时间、后端、模型和提交。
 * 会真的打几次 LLM（两轮对话 + 一次检索重排），花的是真额度。
 */
import { execSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(HERE, '..', 'src', 'test', 'fixtures')
const BASE = (process.env.VITE_API_URL || 'http://127.0.0.1:8080').replace(/\/+$/, '')
const USER = 'u1001'

// 两轮刻意选形状不同的：第一轮抽不到实体（全空数组）+ 报价类触发答案策略；
// 第二轮带订单号（实体非空）+ 订单类。检查器的两条分支都要有真数据。
const TURNS = [
  { name: 'pricing', message: '石英石台面多少钱一米？' },
  { name: 'order',   message: '订单 SO-2026-0912 现在生产到哪一步了？大概什么时候能安排安装？' },
]
const SEARCH_QUERY = '门板缝隙不均能自己调吗'

const json = (o) => JSON.stringify(o, null, 2) + '\n'

async function get(path) {
  const r = await fetch(`${BASE}${path}`)
  if (!r.ok) throw new Error(`GET ${path} → ${r.status} ${await r.text()}`)
  return r.json()
}
async function post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`POST ${path} → ${r.status} ${await r.text()}`)
  return r
}

/** 原样保存 SSE 线格式；顺手从 meta 事件里抠出 conv_id 给下一轮用。 */
async function recordStream(name, message, convId) {
  const body = { message, user_id: USER, ...(convId ? { conv_id: convId } : {}) }
  const raw = await (await post('/chat/stream', body)).text()
  writeFileSync(join(OUT, `chat-stream-${name}.sse`), raw)
  const meta = raw.split('\n\n').find((b) => b.startsWith('event: meta'))
  const data = meta && meta.split('\n').find((l) => l.startsWith('data:'))
  const parsed = data ? JSON.parse(data.slice(5)) : {}
  console.log(`  chat-stream-${name}.sse  ${raw.length} bytes  intent=${parsed.intent} conv=${parsed.conv_id}`)
  return parsed.conv_id
}

async function main() {
  let health
  try {
    health = await get('/health')
  } catch (err) {
    console.error(`后端不可达：${BASE}\n${err.message}\n` +
      '先起后端：env -u ANTHROPIC_BASE_URL ./dev.sh（或 VITE_API_URL 指向别的实例）')
    process.exit(1)
  }
  mkdirSync(OUT, { recursive: true })
  console.log(`录制自 ${BASE}（存储 ${health.storage_backend}）→ ${OUT}`)

  writeFileSync(join(OUT, 'health.json'), json(health))
  console.log('  health.json')

  let convId
  for (const t of TURNS) convId = await recordStream(t.name, t.message, convId)

  const conversation = await get(`/conversations/${encodeURIComponent(convId)}`)
  writeFileSync(join(OUT, 'conversation.json'), json(conversation))
  console.log(`  conversation.json  ${conversation.messages.length} 条`)

  const search = await (await post('/search', { query: SEARCH_QUERY, top_k: 5 })).json()
  writeFileSync(join(OUT, 'search.json'), json(search))
  console.log(`  search.json  ${search.results.length} 条命中 reranked=${search.reranked}`)

  // 两轮对话之后再采 monitor，Agent 统计和告警才是非平凡的
  const monitor = await get('/monitor')
  writeFileSync(join(OUT, 'monitor.json'), json(monitor))
  console.log(`  monitor.json  ${monitor.active_alerts.length} 条告警日志`)

  let commit = 'unknown'
  try { commit = execSync('git rev-parse --short HEAD', { cwd: HERE }).toString().trim() } catch {}
  const model = Object.values(health.agents ?? {})[0]?.model ?? 'unknown'
  writeFileSync(join(OUT, 'manifest.json'), json({
    recorded_at: new Date().toISOString(),
    base_url: BASE,
    storage_backend: health.storage_backend,
    model,
    commit,
    turns: TURNS.map((t) => t.message),
    search_query: SEARCH_QUERY,
  }))
  console.log('  manifest.json')
}

main().catch((err) => { console.error(err); process.exit(1) })
