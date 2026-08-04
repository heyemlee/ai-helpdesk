/**
 * 知识库页：打开就能看到库里有什么，点得开正文，导入后当场找得到。
 * 文档全部来自接口（这里是 mock 的接口），组件里没有任何写死的文档。
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type {
  ImportResult, KnowledgeDocument, KnowledgeDocumentDetail, KnowledgeDocumentList,
} from '@/api/types'

const mocks = vi.hoisted(() => ({
  documents: vi.fn(),
  document: vi.fn(),
  search: vi.fn(),
  addKnowledge: vi.fn(),
  uploadKnowledge: vi.fn(),
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: {
    documents: (_base: string, query: string) => mocks.documents(query),
    document: (_base: string, id: string) => mocks.document(id),
    search: (_base: string, query: string) => mocks.search(query),
    addKnowledge: (_base: string, docs: unknown) => mocks.addKnowledge(docs),
    uploadKnowledge: (_base: string, file: File) => mocks.uploadKnowledge(file),
  },
}))

const { ApiError } = await import('@/api/client')
const { KnowledgePanel } = await import('../KnowledgePanel')

const doc = (over: Partial<KnowledgeDocument>): KnowledgeDocument => ({
  id: 'doc_1', title: 'warranty', display_title: 'Warranty Review and Punch List',
  excerpt: '保修先查该订单的 written limited warranty。', origin: 'original',
  source_type: 'upload', source_name: 'warranty.md', chunk_count: 1, chunks: 1, chars: 426,
  created_at: 1_759_617_315, ...over,
})

const LIBRARY: KnowledgeDocumentList = {
  query: '', matched: 3, total_documents: 3, legacy_documents: 1, total_chunks: 7,
  documents: [
    doc({}),
    doc({ id: 'legacy_2', title: 'install', display_title: 'Cabinet Door Troubleshooting',
          excerpt: '先查柜体水平。', origin: 'legacy', source_type: 'legacy', source_name: '' }),
    doc({ id: 'doc_3', title: 'LTL delivery scope', display_title: 'LTL delivery scope',
          excerpt: 'Curbside delivery does not include installation.', source_type: 'manual',
          source_name: '', chunk_count: 5, chunks: 5 }),
  ],
}

const EMPTY: KnowledgeDocumentList = {
  query: '', documents: [], matched: 0, total_documents: 0, legacy_documents: 0, total_chunks: 0,
}

function show(props: { mode?: 'browse' | 'ask'; docId?: string; lang?: 'en' | 'zh' } = {}) {
  const navigate = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <KnowledgePanel base="/api" mode={props.mode ?? 'browse'} docId={props.docId} navigate={navigate} />,
    {
      lang: props.lang ?? 'en',
      wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    },
  )
  return { navigate, ...view }
}

beforeAll(() => {
  // jsdom 没有实现 <dialog> 的方法；这里只需要它能开能关
  HTMLDialogElement.prototype.showModal = function showModal() { this.open = true }
  HTMLDialogElement.prototype.close = function close() { this.open = false }
})

beforeEach(() => Object.values(mocks).forEach((m) => m.mockReset()))

describe('浏览文档', () => {
  it('不搜索就列出库里的文档，文档数和片段数分开报', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    show()

    expect(await screen.findByRole('link', { name: 'Warranty Review and Punch List' }))
      .toHaveAttribute('href', '#/knowledge/doc/doc_1')
    expect(screen.getByRole('link', { name: 'Cabinet Door Troubleshooting' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'LTL delivery scope' })).toBeInTheDocument()
    expect(mocks.documents).toHaveBeenCalledWith('')        // 打开页面就取，没有先要一个查询词

    const counts = document.querySelector('.kb-counts')!
    expect(counts).toHaveTextContent('3 documents')
    expect(counts).toHaveTextContent('7 passages')
  })

  it('来源和导入时间只显示记下来的：历史文档照实写"未记录"', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    show()
    const cards = await screen.findAllByRole('listitem')
    const [uploaded, legacy, pasted] = cards

    expect(within(uploaded!).getByText('Uploaded file · warranty.md')).toBeInTheDocument()
    expect(within(uploaded!).getByText(/^Imported /)).toBeInTheDocument()
    expect(within(uploaded!).queryByText('Legacy import')).not.toBeInTheDocument()

    expect(within(legacy!).getByText('Legacy import')).toBeInTheDocument()
    expect(within(legacy!).getByText('Source not recorded')).toBeInTheDocument()

    expect(within(pasted!).getByText('Pasted in the workspace')).toBeInTheDocument()
    expect(within(pasted!).getByText('5 passages')).toBeInTheDocument()
  })

  it('加载中、空库、没匹配上、接口出错是四种不同的状态', async () => {
    // 加载中
    let release!: (v: KnowledgeDocumentList) => void
    mocks.documents.mockReturnValueOnce(new Promise<KnowledgeDocumentList>((r) => { release = r }))
    const loading = show()
    expect(screen.getByText('Loading documents…')).toBeInTheDocument()
    // 空库：给导入入口，不是"没有结果"
    release(EMPTY)
    expect(await screen.findByText('The knowledge base is empty')).toBeInTheDocument()
    loading.unmount()

    // 没匹配上：库不是空的，提示换词或改用问题检索
    mocks.documents.mockResolvedValue({ ...LIBRARY, query: 'lifetime', documents: [], matched: 0 })
    const nomatch = show()
    expect(await screen.findByText('No documents contain “lifetime”')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask this as a question' })).toBeInTheDocument()
    expect(screen.queryByText('The knowledge base is empty')).not.toBeInTheDocument()
    nomatch.unmount()

    // 接口出错：说下一步，并留着后端的原话
    mocks.documents.mockRejectedValue(new ApiError(404, '404 Not Found: Not Found'))
    const failed = show()
    expect(await screen.findByText('The document list could not be loaded')).toBeInTheDocument()
    expect(screen.getByText(/Restart the backend/)).toBeInTheDocument()
    expect(screen.getByText('404 Not Found: Not Found')).toBeInTheDocument()
    failed.unmount()

    mocks.documents.mockRejectedValue(new ApiError(401, '401 Unauthorized: 缺少或错误的 X-API-Key'))
    show()
    expect(await screen.findByText(/requires a staff access key/)).toBeInTheDocument()
  })

  it('筛选词发给后端，命中的词在标题和片段里标出来', async () => {
    mocks.documents.mockImplementation(async (query: string) => query
      ? { ...LIBRARY, query, matched: 1, documents: [doc({
          id: 'doc_3', title: 'LTL delivery scope', display_title: 'LTL delivery scope',
          match: { title: true, snippet: '…LTL freight is scheduled separately. Curbside delivery is extra.' },
        })] }
      : LIBRARY)
    show()
    await screen.findByRole('link', { name: 'Warranty Review and Punch List' })

    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter documents by title or text' }), 'LTL  delivery')
    expect(await screen.findByText('1 of 3 documents match “LTL delivery”', { exact: false })).toBeInTheDocument()
    expect(mocks.documents).toHaveBeenLastCalledWith('LTL delivery')    // 多余的空白折掉了
    expect(screen.queryByRole('link', { name: 'Warranty Review and Punch List' })).not.toBeInTheDocument()

    const marks = [...document.querySelectorAll('.doc-card mark')].map((m) => m.textContent)
    expect(marks).toEqual(expect.arrayContaining(['LTL', 'delivery']))

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await screen.findByRole('link', { name: 'Warranty Review and Punch List' })).toBeInTheDocument()
  })

  it('没匹配上时可以把同一句话改用问题检索', async () => {
    mocks.documents.mockImplementation(async (query: string) => query
      ? { ...LIBRARY, query, documents: [], matched: 0 } : LIBRARY)
    mocks.search.mockResolvedValue({ query: 'q', results: [], reranked: false })
    const { navigate } = show()
    await userEvent.type(await screen.findByRole('searchbox'), 'door will not close')
    await userEvent.click(await screen.findByRole('button', { name: 'Ask this as a question' }))
    expect(navigate).toHaveBeenCalledWith('#/knowledge/ask')
    expect(mocks.search).toHaveBeenCalledWith('door will not close')
  })
})

describe('导入', () => {
  const IMPORTED: ImportResult = {
    message: 'ok', added_chunks: 2, added_documents: 1, total_chunks: 9, total_documents: 4,
    documents: [{ id: 'doc_new', title: 'Quartz care', status: 'created', chunks: 2 }],
  }

  it('导入后回到列表，报告结果并给出能直接打开的链接', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.addKnowledge.mockResolvedValue(IMPORTED)
    const { navigate } = show()
    await screen.findByRole('link', { name: 'Warranty Review and Punch List' })

    await userEvent.click(screen.getByRole('button', { name: 'Import document' }))
    const dialog = within(document.querySelector('dialog')!)
    expect(dialog.getByText(/Imported documents are internal/)).toBeInTheDocument()
    expect(dialog.getByLabelText('Title')).toHaveFocus()      // 打开就能直接填，不用先绕过"关闭"
    await userEvent.type(dialog.getByLabelText('Title'), 'Quartz care')
    await userEvent.type(dialog.getByLabelText('Text'), '# Quartz Care\n\nNo sealing needed.')

    // 导入后列表要重新取：新文档得当场出现
    mocks.documents.mockResolvedValue({
      ...LIBRARY, total_documents: 4, matched: 4, total_chunks: 9,
      documents: [doc({ id: 'doc_new', title: 'Quartz care', display_title: 'Quartz Care', chunks: 2, chunk_count: 2 }),
                  ...LIBRARY.documents],
    })
    await userEvent.click(dialog.getByRole('button', { name: 'Import', hidden: true }))

    expect(mocks.addKnowledge).toHaveBeenCalledWith([
      { title: 'Quartz care', content: '# Quartz Care\n\nNo sealing needed.' },
    ])
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent('Imported “Quartz care” as 2 passages.')
    expect(within(notice).getByRole('link', { name: 'Open document' }))
      .toHaveAttribute('href', '#/knowledge/doc/doc_new')
    expect(navigate).toHaveBeenCalledWith('#/knowledge')

    const fresh = (await screen.findByRole('link', { name: 'Quartz Care' })).closest('li')!
    expect(within(fresh).getByText('Just imported')).toBeInTheDocument()
    expect(document.querySelector('.kb-counts')).toHaveTextContent('4 documents')
    expect(document.querySelector('dialog')!.open).toBe(false)
  })

  it('重复导入和空文档照实报告，不说成导入成功', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.uploadKnowledge.mockResolvedValue({
      message: 'ok', added_chunks: 0, added_documents: 0, total_chunks: 7, total_documents: 3,
      documents: [
        { id: 'doc_1', title: 'warranty', status: 'duplicate', chunks: 0 },
        { id: null, title: 'blank', status: 'empty', chunks: 0 },
      ],
    } satisfies ImportResult)
    show()
    await screen.findByRole('link', { name: 'Warranty Review and Punch List' })
    await userEvent.click(screen.getByRole('button', { name: 'Import document' }))

    const file = new File(['[]'], 'batch.json', { type: 'application/json' })
    await userEvent.upload(document.querySelector<HTMLInputElement>('input[type="file"]')!, file)

    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent('“warranty” is already in the knowledge base. Nothing was added.')
    expect(notice).toHaveTextContent('“blank” has no text and was skipped.')
    expect(within(notice).getAllByRole('link', { name: 'Open document' })).toHaveLength(1)
    expect(mocks.uploadKnowledge).toHaveBeenCalledWith(file)
    // 命中的那篇是以前就在的，不能因为这次重复导入就挂上"刚导入"
    expect(screen.queryByText('Just imported')).not.toBeInTheDocument()
  })

  it('导入失败时对话框不关，错误留在原地', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.addKnowledge.mockRejectedValue(new ApiError(503, '503 Service Unavailable: 知识库未初始化'))
    show()
    await screen.findByRole('link', { name: 'Warranty Review and Punch List' })
    await userEvent.click(screen.getByRole('button', { name: 'Import document' }))
    const dialog = within(document.querySelector('dialog')!)
    await userEvent.type(dialog.getByLabelText('Title'), 't')
    await userEvent.type(dialog.getByLabelText('Text'), 'body')
    await userEvent.click(dialog.getByRole('button', { name: 'Import', hidden: true }))

    expect(await dialog.findByText('503 Service Unavailable: 知识库未初始化')).toBeInTheDocument()
    expect(dialog.getByRole('alert', { hidden: true })).toHaveTextContent('The service is still starting up. Try again in a moment.')
    expect(document.querySelector('dialog')!.open).toBe(true)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('服务端读不了上传的文件：说没导入、该查格式，后端原话留着', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.uploadKnowledge.mockRejectedValue(new ApiError(400, '400 Bad Request: JSON 解析失败: Expecting property name'))
    show()
    await screen.findByRole('link', { name: 'Warranty Review and Punch List' })
    await userEvent.click(screen.getByRole('button', { name: 'Import document' }))
    await userEvent.upload(document.querySelector<HTMLInputElement>('input[type="file"]')!,
      new File(['{ broken'], 'broken.json', { type: 'application/json' }))

    const alert = await within(document.querySelector('dialog')!).findByRole('alert', { hidden: true })
    expect(alert).toHaveTextContent('Nothing was imported. The server could not read this input.')
    expect(alert).toHaveTextContent('400 Bad Request: JSON 解析失败: Expecting property name')
    expect(document.querySelector('dialog')!.open).toBe(true)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('按问题检索', () => {
  it('片段标出所在文档并能点过去，证据不足照实说', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.search.mockResolvedValue({
      query: 'q', reranked: true, corrected: true, evidence_ok: false,
      results: [{
        title: 'fulfillment', doc_title: 'Order Status and Delivery', doc_id: 'legacy_9', chunk_id: 'c1',
        chunk: 0, score: 0.327, retrieval: 'hybrid',
        content: '# Order Status and Delivery\n\nLTL freight and installation are separately scheduled.',
      }],
    })
    show({ mode: 'ask' })
    await userEvent.type(screen.getByRole('textbox', { name: 'Ask the knowledge base' }), 'Is installation included?')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByRole('link', { name: 'Order Status and Delivery' }))
      .toHaveAttribute('href', '#/knowledge/doc/legacy_9')
    expect(mocks.search).toHaveBeenCalledWith('Is installation included?')
    expect(screen.getByText(/1 passage retrieved/)).toBeInTheDocument()
    expect(screen.getByText(/judged these insufficient/)).toBeInTheDocument()
    // 片段开头那行和文档标题重复，显示时去掉；正文原样
    const text = document.querySelector('.hit-text')!
    expect(text.textContent).toBe('LTL freight and installation are separately scheduled.')
    expect(text.querySelector('mark')).toHaveTextContent('installation')
  })

  it('Embedding 不可用时片段没有相关度，照样显示并说明是哪一路召回的', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.search.mockResolvedValue({
      query: 'q', reranked: false, corrected: false, evidence_ok: null,
      results: [{
        title: 'fulfillment', doc_title: 'Order Status and Delivery', doc_id: 'legacy_9', chunk_id: 'c1',
        chunk: 0, score: null, retrieval: 'lexical',
        content: 'LTL freight and installation are separately scheduled.',
      }],
    })
    show({ mode: 'ask' })
    await userEvent.type(screen.getByRole('textbox', { name: 'Ask the knowledge base' }), 'Is installation included?')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByRole('link', { name: 'Order Status and Delivery' })).toBeInTheDocument()
    const source = [...document.querySelectorAll('.hit-source')].map((el) => el.textContent)
    expect(source).toContain('lexical')
    expect(screen.queryByText('Relevance')).not.toBeInTheDocument()
  })

  it('没检索过时给示例问法，不显示成"没有结果"', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    show({ mode: 'ask' })
    expect(screen.getByRole('button', { name: 'Does LTL curbside delivery include installation?' })).toBeInTheDocument()
    expect(screen.queryByText('No passages were retrieved')).not.toBeInTheDocument()
    await waitFor(() => expect(mocks.search).not.toHaveBeenCalled())
  })
})

describe('文档正文', () => {
  const DETAIL: KnowledgeDocumentDetail = {
    ...doc({ id: 'legacy_2', title: 'fulfillment', display_title: 'Order Status and Delivery',
             origin: 'legacy', source_type: 'legacy', source_name: '', chunk_count: 2, chunks: 2 }),
    content: '# Order Status and Delivery\n\nLTL freight is scheduled separately.\n\nA schedule date is not a confirmed arrival.',
    body: 'LTL freight is scheduled separately.\n\nA schedule date is not a confirmed arrival.',
    passages: [
      { id: 'chunk-a', index: 0, chars: 36, content: 'LTL freight is scheduled separately.', indexed: true, ingested_at: 1 },
      { id: 'chunk-b', index: 1, chars: 44, content: 'A schedule date is not a confirmed arrival.', indexed: false, ingested_at: 1 },
    ],
  }

  it('先是能读的正文，分片放在次级详情里；历史文档照实标注', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.document.mockResolvedValue(DETAIL)
    show({ docId: 'legacy_2' })

    expect(await screen.findByRole('heading', { name: 'Order Status and Delivery' })).toBeInTheDocument()
    expect(mocks.document).toHaveBeenCalledWith('legacy_2')
    expect(screen.getByRole('link', { name: 'All documents' })).toHaveAttribute('href', '#/knowledge')
    // 正文里不再重复一遍标题
    expect(screen.getAllByText('Order Status and Delivery')).toHaveLength(1)
    expect(document.querySelector('.reader-body')).toHaveTextContent('LTL freight is scheduled separately.')

    expect(screen.getByText('Rebuilt from passages')).toBeInTheDocument()
    expect(screen.getByText(/rebuilt by joining 2 stored passages/)).toBeInTheDocument()
    expect(screen.getByText('Source not recorded')).toBeInTheDocument()
    expect(screen.getByText('fulfillment')).toBeInTheDocument()       // 导入时的标题

    const tech = screen.getByText('Retrieval details').closest('details')!
    expect(tech.open).toBe(false)                                     // 技术信息默认收起
    expect(within(tech).getByText('Passage 1 of 2')).toBeInTheDocument()
    expect(within(tech).getByText('chunk-b')).toBeInTheDocument()
    expect(within(tech).getByText('no keyword index')).toBeInTheDocument()
  })

  it('文档不存在时说清楚，并留着回列表的路', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.document.mockRejectedValue(new ApiError(404, '404 Not Found: 文档不存在: doc_x'))
    show({ docId: 'doc_x' })
    expect(await screen.findByText(/not in the knowledge base/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'All documents' })).toBeInTheDocument()
  })

  it('不完整的历史文档写明缺了多少', async () => {
    mocks.documents.mockResolvedValue(LIBRARY)
    mocks.document.mockResolvedValue({ ...DETAIL, origin: 'legacy_partial', chunk_count: 5, chunks: 2 })
    show({ docId: 'legacy_2' })
    expect(await screen.findByText('Incomplete')).toBeInTheDocument()
    expect(screen.getByText(/Only 2 of 5 passages/)).toBeInTheDocument()
  })
})
