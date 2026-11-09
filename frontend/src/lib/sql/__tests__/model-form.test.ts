import { describe, expect, it } from 'vitest'
import type { SqlModelStatus } from '@/api/sql/types'
import {
  canKeepEmbeddingKey, canKeepKey, embeddingPrefill, embeddingRequestBody, insecurePage, knownRejection,
  modelChoices, originOf, prefill, requestBody,
} from '../modelForm'

/** 移植自 ai-sql-agent/tests/test_console_logic.py 的"模型连接的表单""Embedding 的表单"两组，口径不变 */
const status = {
  provider: 'anthropic',
  providers: {
    anthropic: {
      base_url: 'https://relay.example.com', official_base_url: 'https://api.anthropic.com',
      chat_model: 'claude-sonnet-5', coder_model: 'claude-opus-5-5',
      key_configured: true, key_address: 'https://relay.example.com',
    },
    openai: {
      base_url: 'https://api.openai.com/v1', official_base_url: 'https://api.openai.com/v1',
      chat_model: 'gpt-5.6-luna', coder_model: 'gpt-5.6-luna',
      key_configured: false, key_address: 'https://api.openai.com',
    },
  },
} as unknown as SqlModelStatus

describe('SQL Agent 模型连接的表单', () => {
  it('现有的 Key 只能在同一台主机上接着用', () => {
    const keep = (provider: 'anthropic' | 'openai', baseUrl: string) => canKeepKey(status, { provider, baseUrl })
    const cases = {
      sameHost: keep('anthropic', 'https://relay.example.com/v1'),
      samePadded: keep('anthropic', '  https://RELAY.example.com  '),
      otherHost: keep('anthropic', 'https://other.example.com'),
      lookalike: keep('anthropic', 'https://relay.example.com.evil.example'),
      otherScheme: keep('anthropic', 'http://relay.example.com'),
      otherPort: keep('anthropic', 'https://relay.example.com:8443'),
      blankMeansOfficial: keep('anthropic', ''),
      notAnAddress: keep('anthropic', 'relay'),
      noKeyYet: keep('openai', 'https://api.openai.com/v1'),
      unknownStatus: canKeepKey(null, { provider: 'anthropic', baseUrl: 'https://relay.example.com' }),
    }
    expect(Object.entries(cases).filter(([, value]) => value).map(([name]) => name).sort()).toEqual(['sameHost', 'samePadded'])
    expect([
      'https://Relay.Example.com:443/v1/messages', 'http://localhost:11434/v1', 'http://[::1]:9000',
      'relay.example.com', 'ftp://relay.example.com', '',
    ].map(originOf)).toEqual(['https://relay.example.com', 'http://localhost:11434', 'http://::1:9000', '', '', ''])
  })

  it('换协议时填上那一家现在配的地址和模型', () => {
    expect(prefill(status, 'anthropic')).toEqual({ provider: 'anthropic', apiMode: 'chat', baseUrl: 'https://relay.example.com',
      chatModel: 'claude-sonnet-5', coderModel: 'claude-opus-5-5' })
    const openai = prefill(status, 'openai')
    expect([openai.baseUrl, openai.chatModel]).toEqual(['https://api.openai.com/v1', 'gpt-5.6-luna'])
    expect(prefill(null, 'openai')).toEqual({ provider: 'openai', apiMode: 'chat', baseUrl: '', chatModel: '', coderModel: '' })
  })

  it('只有从别的机器经明文 http 打开的页面才算不加密', () => {
    // 保留这一条：浏览器 → 控制台这一段，模型 Key 照样在请求体里
    expect([
      { protocol: 'http:', hostname: 'localhost' }, { protocol: 'http:', hostname: '127.0.0.1' },
      { protocol: 'http:', hostname: '[::1]' }, { protocol: 'http:', hostname: 'console.localhost' },
      { protocol: 'https:', hostname: 'sql.example.com' },
      { protocol: 'http:', hostname: 'sql.example.com' }, { protocol: 'http:', hostname: '192.168.1.20' },
      { protocol: 'http:', hostname: '127.evil.example' }, { protocol: 'http:', hostname: 'localhost.evil.example' },
    ].map(insecurePage)).toEqual([false, false, false, false, false, true, true, true, true])
  })

  it('请求体的字段都去掉首尾空白；Key 留空表示接着用', () => {
    expect(requestBody({ provider: 'openai', baseUrl: ' https://relay.example.com/v1 ', chatModel: ' some-model ',
      coderModel: '' }, '  sk-typed  ')).toEqual({
      provider: 'openai', api_mode: 'chat', base_url: 'https://relay.example.com/v1', api_key: 'sk-typed',
      chat_model: 'some-model', coder_model: '',
    })
  })

  it('下拉框的候选去重、去空白、按字母排', () => {
    expect(modelChoices(['gpt-6-luna', 'GPT-5.6-luna', ''], 'gpt-6-luna', [' claude-x ', null], []))
      .toEqual(['claude-x', 'GPT-5.6-luna', 'gpt-6-luna'])
    expect(modelChoices([], '')).toEqual([])
  })

  it('只有对凭证的永久拒绝才标在 Key 上', () => {
    const faults = (access_faults: unknown[]) => ({ access_faults }) as unknown as SqlModelStatus
    expect(knownRejection(faults([{ permanent: false, scope: 'model' },
      { permanent: true, scope: 'credential', status_code: 401 }]))?.status_code).toBe(401)
    expect(knownRejection(faults([{ permanent: true, scope: 'model' }, { permanent: false, scope: 'credential' }]))).toBeNull()
    expect(knownRejection({})).toBeNull()
  })
})

describe('SQL Agent Embedding 的表单', () => {
  const defaults = { base_url: 'https://api.openai.com/v1', model: 'text-embedding-3-small', dimensions: 1024 }
  const local = { embedding: { backend: 'local', model: 'local_hash', dimensions: 1024, defaults,
    reusable_key_addresses: ['https://api.openai.com'] } } as unknown as SqlModelStatus
  const remote = { embedding: { backend: 'remote', base_url: 'https://relay.example.com/v1',
    model: 'text-embedding-3-large', dimensions: 3072, defaults,
    reusable_key_addresses: ['https://relay.example.com'] } } as unknown as SqlModelStatus

  it('表单从现在用的远端填起，本地向量时从默认值填起', () => {
    expect(embeddingPrefill(local)).toEqual({ baseUrl: 'https://api.openai.com/v1', model: 'text-embedding-3-small', dimensions: '1024' })
    expect(embeddingPrefill(remote)).toEqual({ baseUrl: 'https://relay.example.com/v1', model: 'text-embedding-3-large', dimensions: '3072' })
    expect(embeddingPrefill(null)).toEqual({ baseUrl: '', model: '', dimensions: '' })
  })

  it('Key 只有在已经有 Key 的主机上才能留空', () => {
    const keep = (state: SqlModelStatus, baseUrl: string) => canKeepEmbeddingKey(state, { baseUrl })
    const cases = {
      chatHostOfficial: keep(local, 'https://api.openai.com/v1'),
      blankMeansOfficial: keep(local, ''),
      otherHost: keep(local, 'https://relay.example.com/v1'),
      currentHost: keep(remote, 'https://relay.example.com'),
      lookalike: keep(remote, 'https://relay.example.com.evil.example/v1'),
      noStatus: canKeepEmbeddingKey(null, { baseUrl: 'https://api.openai.com/v1' }),
    }
    expect(Object.entries(cases).filter(([, value]) => value).map(([name]) => name).sort())
      .toEqual(['blankMeansOfficial', 'chatHostOfficial', 'currentHost'])
  })

  it('请求体去掉空白；维度不是正整数就用默认值', () => {
    expect(embeddingRequestBody({ baseUrl: ' https://api.openai.com/v1 ', model: ' text-embedding-3-small ',
      dimensions: ' 1536 ' }, ' sk-x ')).toEqual({ base_url: 'https://api.openai.com/v1', api_key: 'sk-x',
      model: 'text-embedding-3-small', dimensions: 1536 })
    expect(embeddingRequestBody({ baseUrl: '', model: '', dimensions: 'abc' }, ''))
      .toEqual({ base_url: '', api_key: '', model: '', dimensions: 0 })
    expect(embeddingRequestBody({ baseUrl: '', model: '', dimensions: '12.5' }, '').dimensions).toBe(0)
  })
})
