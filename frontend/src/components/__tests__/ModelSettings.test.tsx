/**
 * 管理与诊断 → 模型连接：现在用的是哪个模型、下拉框换模型并保存、连接设置、测试、恢复，
 * 以及向量切换的进度。接口全部 mock；Key 只往里送，页面上任何地方都不该出现它。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ModelListResult, ModelStatus, ModelTestResult } from '@/api/types'

const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  modelStatus: vi.fn(),
  listChatModels: vi.fn(),
  listEmbeddingModels: vi.fn(),
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
    health: () => mocks.health(),
    modelStatus: () => mocks.modelStatus(),
    listChatModels: (_b: string, body: unknown) => mocks.listChatModels(body),
    listEmbeddingModels: (_b: string, body: unknown) => mocks.listEmbeddingModels(body),
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

const listed = (...models: string[]): ModelListResult =>
  ({ ok: true, models, total: models.length, outcome: 'ok', status: 200, detail: '' })

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
  mocks.health.mockResolvedValue({ status: 'ok', llm: { provider: 'anthropic', model: 'claude-sonnet-5', available: true } })
  mocks.listChatModels.mockResolvedValue(listed('claude-opus-5-5', 'claude-sonnet-5', 'claude-sonnet-5-5'))
  mocks.listEmbeddingModels.mockResolvedValue(listed('text-embedding-3-large', 'text-embedding-3-small'))
  vi.spyOn(window, 'confirm')
})

describe('现在用的是哪个模型', () => {
  it('模型名、协议、主机、Key 末四位和来源都写在卡片最上面', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    show()
    const chat = within(await waitFor(chatCard))
    expect(chat.getByText('In use')).toBeInTheDocument()
    expect(chat.getByText('claude-sonnet-5', { selector: '.model-now-name span' })).toBeInTheDocument()
    expect(chat.getByText('Anthropic protocol')).toBeInTheDocument()
    expect(chat.getByText('relay.example')).toBeInTheDocument()
    expect(chat.getByText('Key ending zz99')).toBeInTheDocument()
    expect(chat.getByText('From server configuration')).toBeInTheDocument()
    expect(await chat.findByText('Available')).toBeInTheDocument()
    expect(within(embeddingCard()).getByText('Local character n-grams (no semantics)')).toBeInTheDocument()
  })

  it('模型调用被保护挡着时，说出原因', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.health.mockResolvedValue({ status: 'ok', llm: { available: false, unavailable_reason: 'credential_rejected' } })
    show()
    const chat = within(await waitFor(chatCard))
    expect(await chat.findByText('Paused')).toBeInTheDocument()
    expect(chat.getByRole('note')).toHaveTextContent('the model provider rejected the API key')
  })

  it('不能改的时候说明原因，只显示现状', async () => {
    mocks.modelStatus.mockResolvedValue({ ...STATUS, editable: false, reason: 'not_local' })
    show()
    expect(await screen.findByText(/only be changed from the console opened on this machine/)).toBeInTheDocument()
    expect(within(chatCard()).getByText('claude-sonnet-5', { selector: '.model-now-name span' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(mocks.listChatModels).not.toHaveBeenCalled()
  })
})

describe('对话模型', () => {
  it('下拉框列出接口提供的模型；同一台主机换模型直接保存，不用重填 Key，也不弹确认', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.saveChatModel.mockResolvedValue({
      ...STATUS,
      chat: { ...STATUS.chat, source: 'page', model: 'claude-opus-5-5', saved_at: '2026-10-14T10:00:00-07:00',
              saved_by: 'console@127.0.0.1' },
      test: OK_CHAT,
    })
    show()
    const card = within(await waitFor(chatCard))
    const select = card.getByLabelText('Model')
    expect(await card.findByText('3 models offered by this endpoint.')).toBeInTheDocument()
    expect(mocks.listChatModels).toHaveBeenCalledWith(
      { provider: 'anthropic', base_url: 'https://relay.example/api', api_key: '' })
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(
      ['claude-opus-5-5', 'claude-sonnet-5', 'claude-sonnet-5-5', 'Other (type a name)…'])

    const save = card.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()                                           // 没改就没什么可存
    await userEvent.selectOptions(select, 'claude-opus-5-5')
    await userEvent.click(save)

    expect(mocks.saveChatModel).toHaveBeenCalledWith({
      provider: 'anthropic', base_url: 'https://relay.example/api', api_key: '', model: 'claude-opus-5-5',
    })
    expect(window.confirm).not.toHaveBeenCalled()
    expect(await card.findByText(/Saved\. Now using claude-opus-5-5\./)).toBeInTheDocument()
    expect(card.getByText('claude-opus-5-5', { selector: '.model-now-name span' })).toBeInTheDocument()
    expect(card.getByText(/Saved in console/)).toBeInTheDocument()
    expect(card.getByLabelText('Model')).toHaveValue('claude-opus-5-5')
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('换成 OpenAI：先要填 Key 才能读列表和保存；换了主机要在页面里确认，保存后 Key 清空', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.listChatModels.mockImplementation(async (body: { provider: string }) => (
      body.provider === 'openai' ? listed('gpt-5.6-luna', 'gpt-6-luna') : listed('claude-sonnet-5')))
    mocks.saveChatModel.mockResolvedValue({
      ...STATUS,
      chat: { ...STATUS.chat, source: 'page', provider: 'openai', base_url: 'https://api.openai.com/v1',
              model: 'gpt-6-luna', key_hint: '1234', saved_at: '2026-10-13T20:00:00-07:00',
              saved_by: 'console@127.0.0.1' },
      test: OK_CHAT,
    })
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.click(card.getByText('Connection settings'))
    await userEvent.selectOptions(card.getByLabelText('Protocol'), 'openai')
    expect(card.getByDisplayValue('https://api.openai.com/v1')).toBeInTheDocument()
    expect(card.getByLabelText('Model')).toHaveValue('gpt-5.6-luna')
    expect(card.getByText(/Enter this endpoint’s key under Connection settings/)).toBeInTheDocument()
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(mocks.listChatModels).not.toHaveBeenCalledWith(expect.objectContaining({ provider: 'openai' }))

    const key = card.getByPlaceholderText('Enter the key for this endpoint')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('autocomplete', 'off')
    await userEvent.type(key, KEY)
    await userEvent.tab()                                                  // 失焦 = 连接定下来了，按它读列表
    await waitFor(() => expect(mocks.listChatModels).toHaveBeenLastCalledWith(
      { provider: 'openai', base_url: 'https://api.openai.com/v1', api_key: KEY }))
    await userEvent.selectOptions(card.getByLabelText('Model'), 'gpt-6-luna')

    await userEvent.click(card.getByRole('button', { name: 'Save' }))
    expect(mocks.saveChatModel).not.toHaveBeenCalled()
    expect(card.getByRole('alertdialog')).toHaveTextContent('will be sent to api.openai.com')
    await userEvent.click(within(card.getByRole('alertdialog')).getByRole('button', { name: 'Save' }))

    expect(mocks.saveChatModel).toHaveBeenCalledWith({
      provider: 'openai', base_url: 'https://api.openai.com/v1', api_key: KEY, model: 'gpt-6-luna',
    })
    expect(await card.findByText(/Saved\. Now using gpt-6-luna\./)).toBeInTheDocument()
    expect((card.getByPlaceholderText('Leave blank to keep the current key') as HTMLInputElement).value).toBe('')
    expect(window.confirm).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(KEY)
  })

  it('读不到模型列表时说原因，可以手动输入模型名', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.listChatModels.mockResolvedValue({
      ok: false, models: [], total: 0, outcome: 'list_unsupported', status: 404, detail: '' })
    mocks.saveChatModel.mockResolvedValue({ ...STATUS, test: OK_CHAT })
    show()
    const card = within(await waitFor(chatCard))
    expect(await card.findByText(/Could not read the model list \(this endpoint does not list its models\)/))
      .toBeInTheDocument()
    await userEvent.selectOptions(card.getByLabelText('Model'), 'Other (type a name)…')
    const typed = card.getByPlaceholderText('Type a model name')
    await userEvent.clear(typed)
    await userEvent.type(typed, 'claude-house-model')
    await userEvent.click(card.getByRole('button', { name: 'Save' }))
    expect(mocks.saveChatModel).toHaveBeenCalledWith(expect.objectContaining({ model: 'claude-house-model' }))
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
    await card.findByText('3 models offered by this endpoint.')
    await userEvent.selectOptions(card.getByLabelText('Model'), 'claude-sonnet-5-5')
    await userEvent.click(card.getByRole('button', { name: 'Save' }))

    expect(await card.findByText('The connection test failed.')).toBeInTheDocument()
    expect(card.getByText(/Chat: Key rejected · HTTP 401/)).toBeInTheDocument()
    await userEvent.click(card.getByRole('button', { name: 'Save anyway' }))
    expect(mocks.saveChatModel).toHaveBeenLastCalledWith(expect.objectContaining({ force: true }))
  })

  it('测试连接在「连接设置」里，结果一行说清', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.testChatModel.mockResolvedValue(OK_CHAT)
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.click(card.getByText('Connection settings'))
    expect(card.getByPlaceholderText('Leave blank to keep the current key')).toBeInTheDocument()
    await userEvent.click(card.getByRole('button', { name: 'Test connection' }))
    expect(mocks.testChatModel).toHaveBeenCalledWith(expect.objectContaining({ api_key: '' }))
    expect(await card.findByText(/The connection works\./)).toHaveTextContent('1030 ms')
  })

  it('换了主机却没填 Key 去测：说清楚为什么要重填', async () => {
    mocks.modelStatus.mockResolvedValue(STATUS)
    mocks.testChatModel.mockRejectedValue(
      new ApiError(422, '422', { code: 'api_key_required', message: '接口地址换了主机或换了协议，要重新填 Key。' }),
    )
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.click(card.getByText('Connection settings'))
    const url = card.getByDisplayValue('https://relay.example/api')
    await userEvent.clear(url)
    await userEvent.type(url, 'https://relay.example:8443/api')
    await userEvent.type(card.getByPlaceholderText('Enter the key for this endpoint'), 'x')
    await userEvent.click(card.getByRole('button', { name: 'Test connection' }))
    expect(await card.findByRole('alert')).toHaveTextContent('different host or protocol, so enter its key')
  })

  it('测试没通过之后恢复为服务端配置：旧的"仍然保存"不能留着，一点就把恢复撤销', async () => {
    const saved = { ...STATUS, chat: { ...STATUS.chat, source: 'page' as const, saved_at: '2026-10-14T10:00:00-07:00' } }
    const failed: ModelTestResult = {
      ok: false, warnings: [],
      checks: [
        { check: 'chat', outcome: 'timeout', status: null, detail: '', latency_ms: 30000 },
        { check: 'structured', outcome: 'skipped', status: null, detail: '', latency_ms: 0 },
      ],
    }
    mocks.modelStatus.mockResolvedValue(saved)
    mocks.saveChatModel.mockRejectedValueOnce(
      new ApiError(422, '422', { code: 'test_failed', message: 'x', test: failed }),
    )
    mocks.clearChatModel.mockResolvedValue(STATUS)
    show()
    const card = within(await waitFor(chatCard))
    await card.findByText('3 models offered by this endpoint.')
    await userEvent.selectOptions(card.getByLabelText('Model'), 'claude-sonnet-5-5')
    await userEvent.click(card.getByRole('button', { name: 'Save' }))
    expect(await card.findByRole('button', { name: 'Save anyway' })).toBeInTheDocument()

    await userEvent.click(card.getByText('Connection settings'))
    await userEvent.click(card.getByRole('button', { name: 'Restore server configuration' }))
    await userEvent.click(within(card.getByRole('alertdialog')).getByRole('button', { name: 'Restore' }))
    expect(await card.findByText('From server configuration')).toBeInTheDocument()

    expect(card.queryByRole('button', { name: 'Save anyway' })).not.toBeInTheDocument()
    expect(mocks.saveChatModel).toHaveBeenCalledTimes(1)       // 只有第一次那个被拒的保存
  })

  it('恢复为服务端配置要在页面里确认', async () => {
    const saved = { ...STATUS, chat: { ...STATUS.chat, source: 'page' as const, saved_at: '2026-10-14T10:00:00-07:00' } }
    mocks.modelStatus.mockResolvedValue(saved)
    mocks.clearChatModel.mockResolvedValue(STATUS)
    show()
    const card = within(await waitFor(chatCard))
    await userEvent.click(card.getByText('Connection settings'))
    await userEvent.click(card.getByRole('button', { name: 'Restore server configuration' }))
    expect(mocks.clearChatModel).not.toHaveBeenCalled()
    await userEvent.click(within(card.getByRole('alertdialog')).getByRole('button', { name: 'Restore' }))
    expect(mocks.clearChatModel).toHaveBeenCalled()
    expect(await card.findByText('From server configuration')).toBeInTheDocument()
  })
})

describe('向量模型', () => {
  it('填好 Key 后从下拉框选模型，确认后开始重建，显示进度和结果', async () => {
    const running: ModelStatus = {
      ...STATUS, switch: { ...STATUS.switch, state: 'running', target_model: 'text-embedding-3-large/3072',
                          target_dim: 3072, total: 58, done: 20, started_at: 'now' },
    }
    mocks.modelStatus.mockResolvedValueOnce(STATUS)
    mocks.saveEmbeddingModel.mockResolvedValue({
      ...running, test: { ok: true, warnings: [], dim: 3072,
                          checks: [{ check: 'embedding', outcome: 'ok', status: 200, detail: '', latency_ms: 200 }] },
    })
    mocks.modelStatus.mockResolvedValue({
      ...STATUS,
      embedding: { ...STATUS.embedding, source: 'page', backend: 'remote', base_url: 'https://api.openai.com/v1',
                   model: 'text-embedding-3-large', dim: 3072, key_set: true, key_hint: '1234' },
      switch: { ...running.switch, state: 'succeeded', done: 58 },
    })
    show()
    const card = within(await waitFor(embeddingCard))
    expect(card.getByText(/Enter this endpoint’s key under Connection settings/)).toBeInTheDocument()
    expect(mocks.listEmbeddingModels).not.toHaveBeenCalled()             // 还没有 Key，不去问
    await userEvent.type(card.getByPlaceholderText('Enter the key for this endpoint'), KEY)
    await userEvent.tab()
    await waitFor(() => expect(mocks.listEmbeddingModels).toHaveBeenCalledWith(
      { base_url: 'https://api.openai.com/v1', api_key: KEY }))
    await userEvent.selectOptions(card.getByLabelText('Model'), 'text-embedding-3-large')
    await userEvent.click(card.getByRole('button', { name: 'Save' }))

    expect(mocks.saveEmbeddingModel).not.toHaveBeenCalled()
    expect(card.getByRole('alertdialog')).toHaveTextContent('re-embedded with text-embedding-3-large')
    await userEvent.click(card.getByRole('button', { name: 'Switch and rebuild' }))
    expect(mocks.saveEmbeddingModel).toHaveBeenCalledWith(expect.objectContaining({
      base_url: 'https://api.openai.com/v1', api_key: KEY, model: 'text-embedding-3-large', dim: 0,
    }))
    expect(await card.findByText('20 / 58')).toBeInTheDocument()
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled()

    expect(await card.findByText('Switched. Rebuilt 58 vectors.', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(card.getByText('text-embedding-3-large', { selector: '.model-now-name span' })).toBeInTheDocument()
    expect(window.confirm).not.toHaveBeenCalled()
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
