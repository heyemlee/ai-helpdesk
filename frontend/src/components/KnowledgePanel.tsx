import { useEffect, useRef, useState } from 'react'
import { useAddKnowledge, useDocuments, useSearch, useUploadKnowledge } from '@/api/hooks'
import { Card, Chip, StateBlock } from './ui'
import { ApiError } from '@/api/client'
import { Icon } from './Icon'
import { KnowledgeDocumentView } from './KnowledgeDocument'
import { HighlightQuestion, HighlightTerms } from '@/lib/highlight'
import { dateTime, num } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { originLabel, sourceLabel } from '@/lib/knowledge'
import { href, type KnowledgeMode } from '@/lib/route'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type { ImportedDocument, ImportResult, KnowledgeDocument } from '@/api/types'

/** 「按问题检索」页的示例问法：点一下就搜。只是问法，不是知识库里的内容。 */
const SAMPLE_KEYS = ['kb.sample.1', 'kb.sample.2', 'kb.sample.3', 'kb.sample.4'] as const

/**
 * 片段正文开头如果就是所在文档的标题行，显示时去掉——标题已经在上面一行了。
 * 只去掉这一种确定的重复，其余原样：这是检索到的原文，不替它润色。
 */
function passageText(content: string, docTitle?: string): string {
  if (!docTitle) return content
  const escaped = docTitle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return content.replace(new RegExp(`^\\s*#{1,6}\\s+${escaped}\\s*#*\\s*(?:。|\\r?\\n)*\\s*`), '')
}

const IMPORT_KEYS: Record<string, Key> = {
  created: 'kb.import.done.created',
  duplicate: 'kb.import.done.duplicate',
  restored: 'kb.import.done.restored',
  empty: 'kb.import.done.empty',
}

/**
 * 知识库。打开就能看到库里有哪些文档，不用先搜。
 *
 * 两件事分开放，因为它们回答的问题不一样：
 *   · 浏览文档 —— "库里有什么"。列表 + 按字面筛选（标题和正文），点开读全文。
 *   · 按问题检索 —— "这个问题会检索到什么"。走和对话同一条检索链路
 *     （改写、语义召回、重排），返回的是片段，每条能点回它所在的文档。
 * 混在一个搜索框里的话，输入一句问题却按字面筛文档，结果是"什么都没有"。
 *
 * 列表、详情、统计全部来自接口。这里没有任何写死的文档。
 */
export function KnowledgePanel({ base, mode, docId, navigate }: {
  base: string
  mode: KnowledgeMode
  docId?: string
  navigate: (to: string) => void
}) {
  const { t, lang } = useT()
  const [filter, setFilter] = useState('')
  const [query, setQuery] = useState('')            // 去抖后真正发给后端的筛选词
  const [question, setQuestion] = useState('')
  const [asked, setAsked] = useState('')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [imported, setImported] = useState<ImportResult | null>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setQuery(filter.trim().replace(/\s+/g, ' ')), 250)
    return () => window.clearTimeout(id)
  }, [filter])

  const docs = useDocuments(base, query)
  const search = useSearch(base)
  const add = useAddKnowledge(base)
  const upload = useUploadKnowledge(base)

  const ask = (q: string) => {
    const text = q.trim()
    if (!text || search.isPending) return
    setQuestion(text)
    setAsked(text)
    search.mutate({ query: text })
  }

  const openImport = () => {
    add.reset(); upload.reset()
    dialogRef.current?.showModal()
    // 原生 dialog 打开时把焦点给第一个能聚焦的元素——这里是右上角的关闭按钮。
    // 打开导入是为了填东西：焦点直接落在标题上，键盘用户不用先绕过"关闭"
    titleRef.current?.focus()
  }
  const closeImport = () => dialogRef.current?.close()

  // 导入成功：关对话框、清筛选（新文档排在最前，得让它露出来）、回到列表并报告结果
  const done = (result: ImportResult) => {
    closeImport()
    setTitle('')
    setContent('')
    setFilter('')
    setQuery('')
    setImported(result)
    navigate(href.knowledge('browse'))
  }

  if (docId) {
    return <KnowledgeDocumentView base={base} docId={docId} />
  }

  const data = docs.data
  const terms = (data?.query ?? '').split(' ').filter(Boolean)
  // 只有这一次真的写进去的才标"刚导入"。重复导入命中的那篇是以前就在的——
  // 报告里说了"没有新增"，卡片上再挂一个"刚导入"就自相矛盾了
  const fresh = new Set((imported?.documents ?? [])
    .filter((d) => d.status === 'created' || d.status === 'restored')
    .map((d) => d.id).filter(Boolean))
  const results = search.data?.results ?? []

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div className="page-heading">
          <div>
            <h2 className="page-title">{t('kb.pageTitle')}</h2>
            <p className="page-lede">{t('kb.lede')} <a href={href.admin('sops')}>{t('kb.lede.sops')}</a></p>
          </div>
          <div className="page-heading-actions">
            <button className="btn btn-primary" onClick={openImport}>
              <Icon name="upload" size={16} />{t('kb.import')}
            </button>
          </div>
        </div>

        {/* 文档数和片段数是两个数：文档是导入的整篇，片段是检索的单位 */}
        {data && (
          <p className="kb-counts" title={t('kb.count.hint')}>
            <strong>{t('kb.count.docs', { n: data.total_documents })}</strong>
            <span>·</span>
            <span>{t('kb.count.passages', { n: data.total_chunks })}</span>
            <span className="kb-counts-hint">{t('kb.count.hint')}</span>
          </p>
        )}

        <div className="tabs" role="tablist" aria-label={t('kb.pageTitle')}>
          {(['browse', 'ask'] as const).map((m) => (
            <a key={m} role="tab" aria-selected={mode === m} href={href.knowledge(m)}
               className={`tab${mode === m ? ' is-active' : ''}`}>
              {t(m === 'browse' ? 'kb.tab.browse' : 'kb.tab.ask')}
            </a>
          ))}
        </div>

        {imported && <ImportNotice result={imported} onDismiss={() => setImported(null)} />}

        {mode === 'browse' ? (
          <section aria-label={t('kb.tab.browse')}>
            <div className="searchbar filterbar">
              <Icon name="search" />
              <label className="sr-only" htmlFor="kb-filter">{t('kb.filter.label')}</label>
              <input
                id="kb-filter" type="search" value={filter} placeholder={t('kb.filter.ph')}
                onChange={(e) => setFilter(e.target.value)}
              />
              {filter && (
                <button className="btn btn-ghost" onClick={() => setFilter('')}>{t('kb.filter.clear')}</button>
              )}
            </div>

            {docs.isError ? (
              (() => {
                const { body, detail } = explainError(docs.error, t, 'kb.error.404')
                return (
                  <StateBlock tone="danger" title={t('kb.error.title')} detail={detail}
                    action={<button className="btn btn-secondary" onClick={() => docs.refetch()}>{t('kb.retry')}</button>}>
                    {body}
                  </StateBlock>
                )
              })()
            ) : !data ? (
              <StateBlock busy title={t('kb.loading')} />
            ) : data.total_documents === 0 ? (
              <StateBlock title={t('kb.empty.title')}
                action={<button className="btn btn-primary" onClick={openImport}>{t('kb.import')}</button>}>
                {t('kb.empty.body')}
              </StateBlock>
            ) : data.documents.length === 0 ? (
              <StateBlock title={t('kb.nomatch.title', { q: data.query })}
                action={
                  <button className="btn btn-secondary" onClick={() => { navigate(href.knowledge('ask')); ask(filter) }}>
                    {t('kb.nomatch.ask')}
                  </button>
                }>
                {t('kb.nomatch.body')}
              </StateBlock>
            ) : (
              <>
                <p className="list-summary" aria-live="polite">
                  {terms.length > 0
                    ? t('kb.filter.summary', { n: data.matched, total: data.total_documents, q: data.query })
                    : t('kb.count.docs', { n: data.total_documents })}
                  {terms.length > 1 && <span> · {t('kb.filter.hint')}</span>}
                  {data.matched > data.documents.length && (
                    <span> · {t('kb.list.truncated', { n: data.documents.length })}</span>
                  )}
                </p>
                <ul className="doc-list">
                  {data.documents.map((doc) => (
                    <DocumentCard key={doc.id} doc={doc} terms={terms} isNew={fresh.has(doc.id)}
                                  date={dateTime(doc.created_at, lang)} />
                  ))}
                </ul>
              </>
            )}
          </section>
        ) : (
          <section aria-label={t('kb.tab.ask')}>
            <p className="tab-lede">{t('kb.ask.lede')}</p>
            <form className="searchbar" onSubmit={(e) => { e.preventDefault(); ask(question) }}>
              <Icon name="search" />
              <label className="sr-only" htmlFor="kb-q">{t('kb.searchLabel')}</label>
              <input
                id="kb-q" value={question} placeholder={t('kb.ask.ph')}
                onChange={(e) => setQuestion(e.target.value)}
                // 同 ChatPanel：组合态的 Enter 是上屏确认，不是检索
                onKeyDown={(e) => { if (e.nativeEvent.isComposing && e.key === 'Enter') e.preventDefault() }}
              />
              <button className="btn btn-primary" type="submit" disabled={search.isPending || !question.trim()} aria-busy={search.isPending}>
                {search.isPending ? t('kb.searching') : t('kb.search')}
              </button>
            </form>

            {search.isError && (() => {
              const { body, detail } = explainError(search.error, t)
              return <StateBlock tone="danger" title={t('kb.ask.error')} detail={detail}>{body}</StateBlock>
            })()}

            {/* 还没问过：给示例问法，不给空白。空白会被读成"搜过了没结果"。 */}
            {search.isIdle && (
              <div className="ask-samples">
                <p className="section-label">{t('kb.try')}</p>
                <div className="suggestions">
                  {SAMPLE_KEYS.map((k) => (
                    <button key={k} className="suggestion" onClick={() => ask(t(k))}>{t(k)}</button>
                  ))}
                </div>
              </div>
            )}

            {search.isPending && <StateBlock busy title={t('kb.ask.running')} />}

            {search.isSuccess && (
              <>
                <p className="list-summary" aria-live="polite">
                  {t('kb.ask.found', { n: results.length })}
                  {search.data.evidence_ok === false && <span> · {t('kb.ask.evidence.weak')}</span>}
                </p>
                {results.length === 0
                  ? <StateBlock title={t('kb.noResults.title')}>{t('kb.noResults')}</StateBlock>
                  : (
                    <ol className="hit-list">
                      {results.map((item, i) => (
                        <li className="hit" key={item.chunk_id ?? i}>
                          <div className="hit-body">
                            <div className="hit-head">
                              {item.doc_id
                                ? <a href={href.document(item.doc_id)}><strong>{item.doc_title || item.title || t('kb.untitled')}</strong></a>
                                : <strong>{item.doc_title || item.title || t('kb.untitled')}</strong>}
                              {item.fallback && <Chip tone="caution">{t('kb.degraded')}</Chip>}
                              {item.chunk !== undefined && !item.fallback && (
                                <span className="hit-source">{t('cw.source.passage', { n: item.chunk + 1 })}</span>
                              )}
                            </div>
                            <p className="hit-text">
                              <HighlightQuestion text={passageText(item.content ?? '', item.doc_title)} query={asked} />
                            </p>
                            {item.score !== undefined && (
                              <p className="hit-source" title={t('kb.relevance.hint')}>
                                {t('kb.relevance')} <span className="mono">{num(item.score, 3)}</span>
                                {item.retrieval && <> · {item.retrieval}</>}
                                {search.data.reranked && <> · {t('kb.reranked')}</>}
                              </p>
                            )}
                          </div>
                        </li>
                      ))}
                    </ol>
                  )}
              </>
            )}
          </section>
        )}

        <dialog className="modal" ref={dialogRef} aria-labelledby="kb-import-title">
          <Card
            title={<span id="kb-import-title">{t('kb.import')}</span>}
            actions={
              <button className="icon-btn" aria-label={t('kb.close')} onClick={closeImport}>
                <Icon name="close" />
              </button>
            }
            bodyStyle={{ display: 'grid', gap: 12 }}
          >
            <p className="note neutral">{t('kb.import.note')}</p>
            <label className="field-row">
              <span>{t('kb.fTitle')}</span>
              <input className="field" ref={titleRef} value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder={t('kb.fTitlePh')} />
            </label>
            <label className="field-row">
              <span>{t('kb.fBody')}</span>
              <textarea className="field" rows={7} value={content} onChange={(e) => setContent(e.target.value)}
                        placeholder={t('kb.fBodyPh')} />
            </label>
            <ImportError error={add.error ?? upload.error} />
            <div className="dialog-actions">
              <button className="btn btn-ghost" disabled={upload.isPending} aria-busy={upload.isPending}
                      onClick={() => fileRef.current?.click()}>
                <Icon name="upload" size={16} />
                {upload.isPending ? t('kb.uploading') : t('kb.upload')}
              </button>
              <span className="spacer" />
              <button className="btn btn-secondary" onClick={closeImport}>{t('kb.cancel')}</button>
              <button
                className="btn btn-primary" aria-busy={add.isPending}
                disabled={add.isPending || !title.trim() || !content.trim()}
                onClick={() => add.mutate([{ title: title.trim(), content }], { onSuccess: done })}
              >
                {add.isPending ? t('kb.importing') : t('kb.doImport')}
              </button>
              <input
                ref={fileRef} type="file" accept=".txt,.md,.json" hidden
                aria-label={t('kb.upload')}
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) upload.mutate(file, { onSuccess: done })
                }}
              />
            </div>
            <p className="field-hint">{t('kb.upload.hint')}</p>
          </Card>
        </dialog>
      </div>
    </div>
  )
}

function DocumentCard({ doc, terms, isNew, date }: {
  doc: KnowledgeDocument; terms: string[]; isNew: boolean; date: string
}) {
  const { t } = useT()
  const origin = originLabel(doc)
  const name = doc.display_title || doc.title || t('kb.untitled')
  // 筛选时优先给命中处的上下文；标题命中而正文没命中时，退回正文开头
  const summary = doc.match?.snippet || doc.excerpt
  return (
    <li className="doc-card">
      <div className="doc-card-head">
        {/* 链接只包标题，整张卡片可点靠 ::after 铺满：读屏听到的链接名是文档标题，
            不是标题 + 摘要 + 来源一长串 */}
        <h3><a href={href.document(doc.id)}><HighlightTerms text={name} terms={terms} /></a></h3>
        {isNew && <Chip tone="success">{t('kb.new')}</Chip>}
        {origin && <Chip tone="caution">{t(origin)}</Chip>}
        {doc.chunks === 0 && <Chip tone="danger">{t('kb.unindexed')}</Chip>}
      </div>
      {summary && (
        <p className="doc-card-text"><HighlightTerms text={summary} terms={terms} /></p>
      )}
      <p className="doc-card-meta">
        {doc.title && doc.title !== name && (
          <span><HighlightTerms text={doc.title} terms={terms} /></span>
        )}
        <span>{sourceLabel(doc, t)}</span>
        {date && <span>{t('kb.meta.imported', { date })}</span>}
        <span>{t('kb.count.passages', { n: doc.chunks })}</span>
      </p>
    </li>
  )
}

/** 导入结果：逐篇说落成了什么，带着能直接打开的链接。 */
/**
 * 导入没成：先说该怎么办，后端的原话跟在后面。
 * 400 在这里只有一种意思——服务端读不了送过去的东西（JSON 解析失败、文件太大、
 * 格式不对），所以单独给一句讲格式的话；其它状态码用通用的说明。
 */
function ImportError({ error }: { error: unknown }) {
  const { t } = useT()
  if (!error) return null
  const { body, detail } = explainError(error, t)
  const rejected = error instanceof ApiError && (error.status === 400 || error.status === 413 || error.status === 422)
  return (
    <p className="note danger" role="alert">
      {rejected ? t('kb.import.rejected') : body}
      <span className="mono state-detail"> {detail}</span>
    </p>
  )
}

function ImportNotice({ result, onDismiss }: { result: ImportResult; onDismiss: () => void }) {
  const { t } = useT()
  const documents: ImportedDocument[] = result.documents ?? []
  return (
    <div className="import-notice" role="status">
      <div>
        {documents.length === 0
          // 老后端只报片段数，不报是哪一篇
          ? <p>{t('kb.import.done.legacy', { n: result.added_chunks })}</p>
          : documents.slice(0, 5).map((doc, i) => (
            <p key={doc.id ?? i}>
              {t(IMPORT_KEYS[doc.status] ?? 'kb.import.done.created', {
                title: doc.title || t('kb.untitled'), n: doc.chunks,
              })}
              {doc.id && <> <a href={href.document(doc.id)}>{t('kb.import.open')}</a></>}
            </p>
          ))}
        {documents.length > 5 && <p>{t('kb.import.done.more', { n: documents.length - 5 })}</p>}
      </div>
      <button className="icon-btn" aria-label={t('kb.close')} onClick={onDismiss}><Icon name="close" size={16} /></button>
    </div>
  )
}
