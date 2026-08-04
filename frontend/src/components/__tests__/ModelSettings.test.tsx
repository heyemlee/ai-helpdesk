/**
 * 管理与诊断 → 模型连接：看现状、测试、保存、恢复，以及向量切换的进度。
 * 接口全部 mock；Key 只往里送，页面上任何地方都不该出现它。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ModelStatus, ModelTestResult } from '@/api/types'

const mocks = vi.hoisted(() => ({
  modelStatus: vi.fn(),
  testChatModel: vi.fn(),
  saveChatModel: vi.fn(),
  clearChatModel: vi.fn(),
  testEmbeddingModel: vi.fn(),
  saveEmbeddingModel: vi.fn(),
  clearEmbeddingModel: vi.fn(),
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: {
    modelStatus: () => mocks.modelStatus(),
    testChatModel: (_b: string, body: unknown) => mocks.testChatModel(body),
    saveChatModel: (_b: string, body: unknown) => mocks.saveChatModel(body),
    clearChatModel: () => mocks.clearChatModel(),
    testEmbeddingModel: (_b: string, body: unknown) => mocks.testEmbeddingModel(body),
    saveEmbeddingModel: (_b: string, body: unknown) => mocks.saveEmbeddingModel(body),
    clearEmbeddingModel: () => mocks.clearEmbeddingModel(),
  },
}))

const { ApiError } = await import('@/api/client')
const { ModelSettings } = await import('../ModelSettings')

const KEY = 'sk-proj-typed-in-the-form-1234'

const STATUS: ModelStatus = {
  enabled: true, editable: true, reason: '',
  chat: {
    source: 'env', provider: 'anthropic', base_url: 'https://relay.example/api', model: 'claude-sonnet-5',
    key_set: true, key_hint: 'zz99', saved_at: '', saved_by: '',
  },
  embedding: {
    source: 'env', backend: 'local_ngram', available: true, base_url: '', model: 'local_ngram', dim: 256,
    query_prefix: '', key_set: false, key_hint: '', saved_at: '', saved_by: '',
  },
  switch: {
    state: 'idle', target_model: '', target_dim: 0, restoring: false, total: 0, done: 0, error: '',
    started_at: '', finished_at: '',
  },
  problems: {},
  defaults: {
    chat: {
      openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-5.6-luna' },
      anthropic: { base_url: 'https://api.anthropic.com', model: 'claude-sonnet-5' },
    },
    embedding: { base_url: 'https://api.openai.com/v1', model: 'text-embedding-3-small', dim: 1536 },
  },
}

const OK_CHAT: ModelTestResult = {
  ok: true, warnings: [],
  checks: [
    { check: 'chat', outcome: 'ok', status: 200, detail: '', latency_ms: 420 },
    { check: 'structured', outcome: 'ok', status: 200, detail: '', latency_ms: 610 },
  ],
}

function show(lang: 'en' | 'zh' = 'en') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ModelSettings base="/api" />, {
    lang,
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  })
}

const chatCard = () => screen.getByRole('heading', { name: 'Chat model' }).closest('section')!
const embeddingCard = () => screen.getByRole('heading', { name: 'Embedding model' }).closest('section')!

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset())
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

describe('现状', () => {
  it('说清楚现在用的是哪一份，Key 只报配没配和末四位', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    show()
    const chat = within(await waitFor(chatCard))
    expect(chat.getByText('Server configuration')).toBeInTheDocument()
    expect(chat.getByText('claude-sonnet-5', { selector: 'span' })).toBeInTheDocument()
    expect(chat.getByText('Set · ends in zz99')).toBeInTheDocument()
    expect(within(embeddingCard()).getByText('Local character n-grams, no semantics')).toBeInTheDocument()
  })

  it('不能改的时候说明原因，不显示表单', async () => {
    mocks.modelStatus.mockResolvedValue({ ...STATUS, editable: false, reason: 'not_local' })
    show()
    expect(await screen.findByText(/only be changed from the console opened on this machine/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument()
  })
})

describe('对话模型', () => {
  it('换成 OpenAI：地址和模型换成它的默认值，Key 必须重填，保存后输入框清空', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.saveChatModel.mockResolvedValue({
      ...STATUS,
      chat: { ...STATUS.chat, source: 'page', provider: 'openai', base_url: 'https://api.openai.com/v1',
              model: 'gpt-5.6-luna', key_hint: '1234', saved_at: '2026-10-13T20:00:00-07:00',
              saved_by: 'console@127.0.0.1' },
      test: OK_CHAT,
    })
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.selectOptions(card.getByLabelText('Protocol'), 'openai')
    expect(card.getByDisplayValue('https://api.openai.com/v1')).toBeInTheDocument()
    expect(card.getByDisplayValue('gpt-5.6-luna')).toBeInTheDocument()
    const key = card.getByPlaceholderText('Enter the key for this endpoint')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('autocomplete', 'off')

    await userEvent.type(key, KEY)
    await userEvent.click(card.getByRole('button', { name: 'Save' }))

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('api.openai.com'))
    expect(mocks.saveChatModel).toHaveBeenCalledWith({
      provider: 'openai', base_url: 'https://api.openai.com/v1', api_key: KEY, model: 'gpt-5.6-luna',
    })
    expect(await card.findByText('The connection works.')).toBeInTheDocument()
    expect(card.getByText('Saved in console')).toBeInTheDocument()
    expect((card.getByPlaceholderText(/Leave blank to keep the current key/) as HTMLInputElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain(KEY)
  })

  it('同一个主机可以不重填 Key', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.testChatModel.mockResolvedValue(OK_CHAT)
    show()
    const card = within(await waitFor(chatCard))
    expect(card.getByPlaceholderText('Leave blank to keep the current key')).toBeInTheDocument()
    await userEvent.click(card.getByRole('button', { name: 'Test connection' }))
    expect(mocks.testChatModel).toHaveBeenCalledWith(expect.objectContaining({ api_key: '' }))
    expect(await card.findByText(/Structured output: OK/)).toBeInTheDocument()
  })

  it('测试没通过就不保存，给出原因和"仍然保存"', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    const failed: ModelTestResult = {
      ok: false, warnings: [],
      checks: [
        { check: 'chat', outcome: 'auth_failed', status: 401, detail: 'Incorrect API key provided: sk-pr***', latency_ms: 90 },
        { check: 'structured', outcome: 'skipped', status: null, detail: '', latency_ms: 0 },
      ],
    }
    mocks.saveChatModel
      .mockRejectedValueOnce(new ApiError(422, '422', { code: 'test_failed', message: 'x', test: failed }))
      .mockResolvedValueOnce({ ...STATUS, test: failed })
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.click(card.getByRole('button', { name: 'Save' }))

    expect(await card.findByText('The connection test failed.')).toBeInTheDocument()
    expect(card.getByText(/Chat: Key rejected · HTTP 401/)).toBeInTheDocument()
    await userEvent.click(card.getByRole('button', { name: 'Save anyway' }))
    expect(mocks.saveChatModel).toHaveBeenLastCalledWith(expect.objectContaining({ force: true }))
  })

  it('换了主机却没填 Key：说清楚为什么要重填', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.testChatModel.mockRejectedValue(
      new ApiError(422, '422', { code: 'api_key_required', message: '接口地址换了主机或换了协议，要重新填 Key。' }),
    )
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.selectOptions(card.getByLabelText('Protocol'), 'openai')
    await userEvent.click(card.getByRole('button', { name: 'Test connection' }))
    expect(await card.findByRole('alert')).toHaveTextContent('different host or protocol, so enter its key')
  })
})

describe('向量模型', () => {
  it('保存后显示重建进度，完成后报重建了多少个', async () => {
    const running: ModelStatus = {
      ...STATUS, switch: { ...STATUS.switch, state: 'running', target_model: 'text-embedding-3-small/1536',
                          target_dim: 1536, total: 58, done: 20, started_at: 'now' },
    }
    mocks.modelStatus.mockResolvedValueOnce(STATUS)
    mocks.saveEmbeddingModel.mockResolvedValue({
      ...running, test: { ok: true, warnings: [], dim: 1536,
                          checks: [{ check: 'embedding', outcome: 'ok', status: 200, detail: '', latency_ms: 200 }] },
    })
    mocks.modelStatus.mockResolvedValue({
      ...STATUS,
      embedding: { ...STATUS.embedding, source: 'page', backend: 'remote', base_url: 'https://api.openai.com/v1',
                   model: 'text-embedding-3-small', dim: 1536, key_set: true, key_hint: '1234' },
      switch: { ...running.switch, state: 'succeeded', done: 58 },
    })
    show()
    const card = within(await waitFor(embeddingCard))
    expect(card.getByDisplayValue('text-embedding-3-small')).toBeInTheDocument()
    await userEvent.type(card.getByPlaceholderText('Enter the key for this endpoint'), KEY)
    await userEvent.click(card.getByRole('button', { name: 'Save and rebuild vectors' }))

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('text-embedding-3-small'))
    expect(mocks.saveEmbeddingModel).toHaveBeenCalledWith(expect.objectContaining({
      base_url: 'https://api.openai.com/v1', api_key: KEY, model: 'text-embedding-3-small', dim: 0,
    }))
    expect(await card.findByText('20 / 58')).toBeInTheDocument()
    expect(card.getByRole('button', { name: 'Save and rebuild vectors' })).toBeDisabled()

    expect(await card.findByText('Switched. Rebuilt 58 vectors.', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain(KEY)
  })

  it('切换失败时说清楚库里没有改动', async () => {
    mocks.modelStatus.mockResolvedValue({
      ...STATUS, switch: { ...STATUS.switch, state: 'failed', error: 'EmbeddingUnavailable: HTTP 503' },
    })
    show()
    const card = within(await waitFor(embeddingCard))
    expect(card.getByRole('alert')).toHaveTextContent('nothing changed: EmbeddingUnavailable: HTTP 503')
  })
})
