/**
 * 真后端录制的夹具（src/test/fixtures/，由 scripts/record-fixtures.mjs 生成）。
 *
 * 这里只做读取和回放，不修饰形状：空数组的实体键、134 字符不换行的 routing_reason、
 * 逐句计的 checked_total，都原样进测试——这些正是手写夹具从来没有过的东西。
 */
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parseSseBlock } from '@/api/client'
import { applyStreamEvent, type ChatMessage } from '@/hooks/useChatStream'

// vitest 的 cwd 就是 vite root（frontend/），不依赖 import.meta.url 的 scheme
const DIR = resolve(process.cwd(), 'src', 'test', 'fixtures')

export const fixture = (name: string): string => readFileSync(join(DIR, name), 'utf8')
export const fixtureJson = <T,>(name: string): T => JSON.parse(fixture(name)) as T

/** 把录下来的 SSE 原文按 useChatStream 同一套归约折成一条助手消息。 */
export function foldStream(name: string): ChatMessage {
  let m: ChatMessage = { id: name, role: 'assistant', content: '' }
  for (const block of fixture(name).split('\n\n')) {
    const evt = parseSseBlock(block)
    if (evt) m = applyStreamEvent(m, evt)
  }
  return m
}
