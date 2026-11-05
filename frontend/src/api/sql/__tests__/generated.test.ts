// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { describe, expect, it } from 'vitest'

// vitest 在 frontend/ 下跑
const FRONTEND = process.cwd()

/**
 * generated.ts 必须就是生成脚本现在的输出。
 *
 * 链条是：SQL Agent 改了接口 → ai-sql-agent 的 test_openapi_snapshot 逼着重新导出 openapi.json →
 * 这条用例逼着重新生成前端类型 → 字段改名、删字段、改类型的地方，前端的类型检查报出来。
 * 契约的 JSON Schema 同理。少了哪一环，"接口变了前端不知道"就又回来了。
 */
describe('SQL Agent 和 A2A 契约的生成类型', () => {
  it('提交进去的 generated.ts 和生成脚本的输出一字不差', async () => {
    const script = pathToFileURL(path.join(FRONTEND, 'scripts', 'gen-sql-types.mjs')).href
    const generator = await import(/* @vite-ignore */ script) as { render: () => string; OUTPUT: string }
    const committed = readFileSync(generator.OUTPUT, 'utf8')
    expect(committed, 'generated.ts 过期了：跑 node frontend/scripts/gen-sql-types.mjs').toBe(generator.render())
  })

  it('请求体、历史和契约的几个关键类型都在', async () => {
    const committed = readFileSync(path.join(FRONTEND, 'src', 'api', 'sql', 'generated.ts'), 'utf8')
    for (const name of ['PreviewRequest', 'ProfileRequest', 'QueryResponse', 'HistoryItem', 'FeedbackRequest',
      'ArtifactDataPart', 'ProgressEvent', 'TerminalEvent', 'Stage']) {
      expect(committed).toMatch(new RegExp(`export (interface|type) ${name}\\b`))
    }
  })
})
