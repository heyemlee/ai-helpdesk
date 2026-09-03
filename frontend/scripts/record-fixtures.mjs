#!/usr/bin/env node
/**
 * 从真后端录制测试夹具。
 *
 * 为什么要录而不是手写：手写的 meta/routing/monitor 对象永远长不成真后端的样子——
 * 空数组的实体键、一行不换行的 routing_reason、每 10s 复发一条的告警日志、带负分的
 * 检索结果，这些形状全是接了真后端才现形的，而手写夹具一个都没有。录下来的文件
 * 就是当时的线上形状；后端字段一变，重录一次，前端测试自动知道。
 *
 * 中英各录一组：回复语言跟客户走之后，英文是一条独立的渲染路径——英文正文配
 * 中文诊断串、英文转人工通知、markdown 结构也不一样。只录中文等于这条路径
 * 一行测试都没有。
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

// 每种语言两轮，刻意选形状不同的：第一轮抽不到实体（全空数组）+ 报价类触发
// 答案策略；第二轮带订单号（实体非空）+ 订单类。检查器的两条分支都要有真数据。
//
// 英文那组额外覆盖：英文正文 + 中文诊断串的混排、英文转人工通知、
// 以及英文提问下 intent_strategy 更常落在 ensemble（英文锚点比中文稀）。
const SUITES = [
  {
    lang: 'zh',
    turns: [
      { name: 'pricing', message: '石英石台面多少钱一米？' },
      { name: 'order',   message: '订单 SO-2026-0912 现在生产到哪一步了？大概什么时候能安排安装？' },
    ],
    search: '门板缝隙不均能自己调吗',
  },
  {
    lang: 'en',
    turns: [
      { name: 'pricing', message: 'How much is a quartz countertop per metre?' },
      { name: 'order',   message: 'Where is order SO-2026-0912, and when can installation be scheduled?' },
    ],
    search: 'can I adjust an uneven door gap myself',
  },
]

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
async function recordStream(lang, name, message, convId) {
  const body = { message, user_id: `${USER}-${lang}`, ...(convId ? { conv_id: convId } : {}) }
  const raw = await (await post('/chat/stream', body)).text()
  writeFileSync(join(OUT, `${lang}-chat-stream-${name}.sse`), raw)
  const meta = raw.split('\n\n').find((b) => b.startsWith('event: meta'))
  const data = meta && meta.split('\n').find((l) => l.startsWith('data:'))
  const parsed = data ? JSON.parse(data.slice(5)) : {}
  console.log(`  ${lang}-chat-stream-${name}.sse  ${raw.length} bytes  `
    + `intent=${parsed.intent} strategy=${parsed.intent_strategy}`)
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

  for (const suite of SUITES) {
    let convId
    for (const t of suite.turns) {
      convId = await recordStream(suite.lang, t.name, t.message, convId)
    }
    // 审计日志是流结束之后才落库的，紧接着 GET 会拿到少一条的快照
    // （实测中文录到 4 条、英文只有 3 条——同一个脚本，纯粹是时序）。
    // 等到条数达到预期（每轮一问一答）或不再增长为止。
    const want = suite.turns.length * 2
    let conversation = { messages: [] }
    for (let i = 0; i < 10; i++) {
      const next = await get(`/conversations/${encodeURIComponent(convId)}`)
      if (next.messages.length >= want) { conversation = next; break }
      if (i && next.messages.length === conversation.messages.length) { conversation = next; break }
      conversation = next
      await new Promise((r) => setTimeout(r, 500))
    }
    if (conversation.messages.length < want) {
      console.warn(`  ! ${suite.lang}-conversation 只拿到 ${conversation.messages.length}/${want} 条`)
    }
    writeFileSync(join(OUT, `${suite.lang}-conversation.json`), json(conversation))
    console.log(`  ${suite.lang}-conversation.json  ${conversation.messages.length} 条`)

    const search = await (await post('/search', { query: suite.search, top_k: 5 })).json()
    writeFileSync(join(OUT, `${suite.lang}-search.json`), json(search))
    console.log(`  ${suite.lang}-search.json  ${search.results.length} 条命中 reranked=${search.reranked}`)
  }

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
    suites: SUITES.map((s) => ({ lang: s.lang, turns: s.turns.map((t) => t.message), search: s.search })),
  }))
  console.log('  manifest.json')
}

main().catch((err) => { console.error(err); process.exit(1) })
