/**
 * 排队模式（后端 AI_HELPDESK_RUN_MODE=queued）下的对话：
 *
 * - 帧带 id，断线之后带着最后一个 id（Last-Event-ID）去 /runs/{run_id}/events 接着读，
 *   客户看到的回复不缺不重；
 * - 处理它的实例失联、换人重跑：attempt 帧之后上一遍的半截清掉，从头再收；
 * - 后端在流里报的错是这一轮的结局，不续流；inline 后端没有 run 帧，断了就是断了；
 * - 气泡和学习模式只转述后端报的：第几次处理、按哪一档精简处理。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { render, screen } from '@/test/render'
import { parseSseBlock } from '@/api/client'
import { applyStreamEvent, useChatStream, type ChatMessage } from '@/hooks/useChatStream'
import { MessageBubble } from '../MessageBubble'
import { TurnExplanation } from '../TurnExplanation'

const frame = (event: string, data: unknown, id?: string) =>
  `${id ? `id: ${id}\n` : ''}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

const RUN = '01a15d164e171996708ba977796eabcf'
const META = { conv_id: 'c1', turn_id: 't1', intent: 'missing_part', intent_group: 'order',
               intent_confidence: 0.9, intent_strategy: 'ensemble', intent_latency_ms: 1,
               intent_source_scores: {}, entities: {}, knowledge_used: false,
               run: { run_id: RUN, attempt: 1 }, degrade: { level: 0, backlog: 0, threshold: 0, off: [] as string[] } }
const ROUTING = { primary_agent: 'order', supporting_agents: [], reason: 'r', confidence: 0.9 }
const RESULT = { conv_id: 'c1', response: '补件已经登记。', agent_type: 'order', agent_types: ['order'],
                 primary_agent: 'order', supporting_agents: [], routing_reason: 'r', routing_confidence: 0.9,
                 escalated: false, latency_ms: 10, handoff: false, handoff_reason: '',
                 needs_clarification: false, answer_confidence: 0.9, policy_violations: [],
                 run: { run_id: RUN, attempt: 1 }, degrade: { level: 0, backlog: 0, threshold: 0, off: [] as string[] } }

/**
 * 一个 SSE 响应：一帧一帧地发，发完之后 breakAfter 为真就模拟网络断开（读流时抛 TypeError）。
 * 按 pull 发而不是一次性塞进队列：流一出错，队列里还没读的帧会被整个丢掉——真断网时
 * 已经收到的那些帧是读得到的。
 */
function sseResponse(frames: string[], breakAfter = false): Response {
  const encoder = new TextEncoder()
  const queue = [...frames]
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = queue.shift()
      if (next !== undefined) controller.enqueue(encoder.encode(next))
      else if (breakAfter) controller.error(new TypeError('network error'))
      else controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const settings = { userId: 'u1', conversationId: '', apiBaseUrl: '/api' }

async function send(text = '补件到哪了') {
  const onConversation = vi.fn()
  const hook = renderHook(() => useChatStream(settings, onConversation))
  await act(async () => { await hook.result.current.send(text, true) })
  return hook
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('SSE 的 id', () => {
  it('带 id 的帧解析出 id，不带的照旧', () => {
    expect(parseSseBlock('id: 17-0\nevent: delta\ndata: {"text":"你好"}')).toEqual(
      { event: 'delta', data: { text: '你好' }, id: '17-0' })
    expect(parseSseBlock('event: delta\ndata: {"text":"你好"}')).toEqual({ event: 'delta', data: { text: '你好' } })
  })
})

describe('断线续流', () => {
  it('流断了：带着最后一个 id 去 /runs/{run_id}/events 接着读，回复不缺不重', async () => {
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => url.endsWith('/chat/stream')
      ? sseResponse([frame('run', { run_id: RUN, conv_id: 'c1', status: 'queued', attempt: 0 }),
                     frame('attempt', { run_id: RUN, attempt: 1, reclaimed: false }, '1-0'),
                     frame('meta', META, '2-0'),
                     frame('routing', ROUTING, '3-0'),
                     frame('delta', { text: '补件' }, '4-0')], true)
      : sseResponse([frame('delta', { text: '已经登记。' }, '5-0'), frame('result', RESULT, '6-0')]))
    vi.stubGlobal('fetch', fetchMock)

    const hook = await send()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [url, init] = fetchMock.mock.calls[1]!
    expect(url).toBe(`/api/runs/${RUN}/events`)
    expect((init!.headers as Record<string, string>)['Last-Event-ID']).toBe('4-0')
    const first = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>
    expect(first['Idempotency-Key']).toMatch(/.+/)              // 提交时带着幂等键

    const answer = hook.result.current.messages.at(-1)!
    expect(answer.content).toBe('补件已经登记。')
    expect(answer.error).toBeUndefined()
    expect(answer.runId).toBe(RUN)
    expect(answer.reconnecting).toBe(false)
  })

  it('续流时 API 正在重启、代理先回了 503：接着重试，等它起来', async () => {
    let calls = 0
    const fetchMock = vi.fn(async (url: string) => {
      calls += 1
      if (url.endsWith('/chat/stream')) {
        return sseResponse([frame('run', { run_id: RUN, conv_id: 'c1', status: 'queued', attempt: 0 }),
                            frame('delta', { text: '补件' }, '1-0')], true)
      }
      if (calls === 2) return new Response('Service Unavailable', { status: 503, statusText: 'Service Unavailable' })
      return sseResponse([frame('delta', { text: '已经登记。' }, '2-0'), frame('result', RESULT, '3-0')])
    })
    vi.stubGlobal('fetch', fetchMock)
    const hook = await send()
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(hook.result.current.messages.at(-1)!.content).toBe('补件已经登记。')
    expect(hook.result.current.messages.at(-1)!.error).toBeUndefined()
  })

  it('run 已经不在了（404）：不再重试，如实报错', async () => {
    const fetchMock = vi.fn(async (url: string) => url.endsWith('/chat/stream')
      ? sseResponse([frame('run', { run_id: RUN, conv_id: 'c1', status: 'queued', attempt: 0 })], true)
      : new Response(JSON.stringify({ detail: 'run 不存在或已超出保留期' }), { status: 404 }))
    vi.stubGlobal('fetch', fetchMock)
    const hook = await send()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(hook.result.current.messages.at(-1)!.error).toMatch(/不存在/)
  })

  it('inline 的后端没有 run 帧：断了就报错，不去续流', async () => {
    const fetchMock = vi.fn(async () => sseResponse([frame('meta', { ...META, run: undefined })], true))
    vi.stubGlobal('fetch', fetchMock)
    const hook = await send()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(hook.result.current.messages.at(-1)!.error).toMatch(/network error/)
  })

  it('后端在流里报的错是这一轮的结局，不续流', async () => {
    const fetchMock = vi.fn(async () => sseResponse([
      frame('run', { run_id: RUN, conv_id: 'c1', status: 'queued', attempt: 0 }),
      frame('error', { detail: '编排器未返回结果' }, '1-0')]))
    vi.stubGlobal('fetch', fetchMock)
    const hook = await send()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(hook.result.current.messages.at(-1)!.error).toBe('编排器未返回结果')
  })

  it('重连几次都不成：如实报错', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (url: string) => url.endsWith('/chat/stream')
      ? sseResponse([frame('run', { run_id: RUN, conv_id: 'c1', status: 'queued', attempt: 0 })], true)
      : Promise.reject(new TypeError('Failed to fetch')))
    vi.stubGlobal('fetch', fetchMock)
    const hook = renderHook(() => useChatStream(settings, vi.fn()))
    let sending: Promise<void> = Promise.resolve()
    await act(async () => { sending = hook.result.current.send('补件到哪了', true) })
    for (let i = 0; i < 20; i += 1) {                            // 0.5 + 1 + 2 + 4 + 8 秒
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    }
    await act(async () => { await sending })
    expect(fetchMock).toHaveBeenCalledTimes(6)                   // 一次提交 + 五次续流
    expect(hook.result.current.busy).toBe(false)
    expect(hook.result.current.messages.at(-1)!.error).toMatch(/重连/)
  })
})

describe('换人重跑', () => {
  it('第 2 次执行开始时清掉上一遍的半截，从头再收', () => {
    let m: ChatMessage = { id: 'a', role: 'assistant', content: '' }
    m = applyStreamEvent(m, { event: 'attempt', data: { run_id: RUN, attempt: 1, reclaimed: false } })
    m = applyStreamEvent(m, { event: 'meta', data: META as never })
    m = applyStreamEvent(m, { event: 'routing', data: ROUTING })
    m = applyStreamEvent(m, { event: 'delta', data: { text: '第一遍说到一半' } })
    m = applyStreamEvent(m, { event: 'attempt', data: { run_id: RUN, attempt: 2, reclaimed: true,
                                                         degrade: { level: 1, backlog: 30, threshold: 20, off: ['tool_loop'] } } })
    expect(m).toMatchObject({ content: '', attempt: 2, meta: undefined, routing: undefined, progress: [] })
    expect(m.degrade?.level).toBe(1)
    m = applyStreamEvent(m, { event: 'delta', data: { text: '第二遍。' } })
    expect(m.content).toBe('第二遍。')
  })
})

describe('气泡和学习模式里的说明', () => {
  const answered = (over: Partial<typeof RESULT>): ChatMessage => ({
    id: 'a', role: 'assistant', content: '补件已经登记。', routing: ROUTING,
    meta: META as never, result: { ...RESULT, ...over } as never,
  })

  it('换人处理过的一轮：说明是第几次处理', () => {
    render(<MessageBubble message={answered({ run: { run_id: RUN, attempt: 2 } })} />)
    expect(screen.getByText(/另一个实例接手（第 2 次处理）/)).toBeInTheDocument()
  })

  it('精简处理的一轮：说关掉了哪些环节，不说内部档位名', () => {
    render(<MessageBubble message={answered({ degrade: { level: 2, backlog: 45, threshold: 40,
                                                         off: ['tool_loop', 'rewrite_rerank'] } })} />)
    expect(screen.getByText('当前咨询量较大，这个问题按精简方式处理：不追加查询；知识库只做一次直接检索。')).toBeInTheDocument()
  })

  it('inline 的一轮（没有 run、没有降级）什么都不多说', () => {
    render(<MessageBubble message={answered({ run: undefined, degrade: undefined })} />)
    expect(screen.queryByText(/接手|精简/)).not.toBeInTheDocument()
  })

  it('正在续流时提示连接断了', () => {
    render(<MessageBubble message={{ ...answered({}), streaming: true, reconnecting: true }} />, { lang: 'en' })
    expect(screen.getByText('The connection dropped. Picking this question back up…')).toBeInTheDocument()
  })

  it('学习模式讲清楚处理了几次、是不是按提交点收的尾、当时积压多少', () => {
    const resumed = answered({ run: { run_id: RUN, attempt: 2, resumed_from: 't1' } as never,
                               degrade: { level: 3, backlog: 90, threshold: 80,
                                          off: ['tool_loop', 'rewrite_rerank', 'order_data'] } })
    render(<TurnExplanation message={resumed} />)
    expect(screen.getByText('排队')).toBeInTheDocument()
    expect(screen.getByText(/处理了 2 次/)).toBeInTheDocument()
    expect(screen.getByText(/发出的是同一个回答/)).toBeInTheDocument()
    expect(screen.getByText(/当时积压 90 条（第 3 档从 80 条开始）/)).toBeInTheDocument()
  })
})
