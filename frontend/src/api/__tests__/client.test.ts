import { describe, expect, it } from 'vitest'
import { parseSseBlock } from '../client'

describe('parseSseBlock', () => {
  it('解析标准 SSE 块', () => {
    const evt = parseSseBlock('event: delta\ndata: {"text":"你好"}')
    expect(evt).toEqual({ event: 'delta', data: { text: '你好' } })
  })

  it('没有 event 名时返回 null，避免把心跳当成数据', () => {
    expect(parseSseBlock('data: {"text":"x"}')).toBeNull()
  })

  it('data 不是合法 JSON 时返回 null 而不是抛异常', () => {
    expect(parseSseBlock('event: delta\ndata: {不是json')).toBeNull()
  })

  it('忽略空块', () => {
    expect(parseSseBlock('')).toBeNull()
    expect(parseSseBlock('\n\n')).toBeNull()
  })

  it('支持多行 data', () => {
    const evt = parseSseBlock('event: result\ndata: {"a":1,\ndata: "b":2}')
    expect(evt?.data).toEqual({ a: 1, b: 2 })
  })
})
