import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useAddKnowledge, useCategories, useDocuments, useSearch, useUploadKnowledge } from '@/api/hooks'
import { Card, Chip, StateBlock } from './ui'
import { ApiError } from '@/api/client'
import { Icon } from './Icon'
import { KnowledgeDocumentView } from './KnowledgeDocument'
import { HighlightQuestion, HighlightTerms } from '@/lib/highlight'
import { num } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { UNCATEGORIZED, categoryName, groupByCategory, originLabel } from '@/lib/knowledge'
import { href, type KnowledgeMode } from '@/lib/route'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type {
  ImportedDocument, ImportResult, KnowledgeCategory, KnowledgeDocument, KnowledgeDocumentList,
} from '@/api/types'

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
 * 导入完去哪儿看：这一批都落在同一个分类里，就打开那个分类（未分类也算一类）——
 * 按分类分组之后，新文档不一定排在"全部"的最上面，得把人带到它所在的地方。
 * 散在几个分类里、或者后端不报分类，回到全部。
 */
function landingFor(result: ImportResult): string {
  const filed = (result.documents ?? []).filter((d) => d.id && d.category !== undefined)
  const [only, ...others] = new Set(filed.map((d) => d.category || UNCATEGORIZED))
  return only && others.length === 0 ? href.category(only) : href.knowledge('browse')
}

/**
 * 知识库。打开就能看到库里有哪些文档，不用先搜。
 *
 * 两件事分开放，因为它们回答的问题不一样：
 *   · 浏览文档 —— "库里有什么"。左边是分类目录（按客户从咨询到售后的顺序），
 *     右边是文档：看全部时按分类分组，选了分类就只列这一类。筛选词（标题和正文）
 *     和分类叠加，目录上的数字是每类里匹配的篇数。点开读全文。
 *   · 按问题检索 —— "这个问题会检索到什么"。走和对话同一条检索链路
 *     （改写、语义召回、重排），返回的是片段，每条能点回它所在的文档。
 * 混在一个搜索框里的话，输入一句问题却按字面筛文档，结果是"什么都没有"。
 *
 * 分类只用来整理，检索不看它。列表、分类、详情、统计全部来自接口，这里没有
 * 任何写死的文档或分类；后端还没有分类时（老版本），退回不分组的列表。
 */
export function KnowledgePanel({ base, mode, docId, category, navigate }: {
  base: string
  mode: KnowledgeMode
  docId?: string
  /** 地址里选中的分类 key；不给是全部，`uncategorized` 是未分类 */
  category?: string
  navigate: (to: string) => void
}) {
  const { t, lang } = useT()
  const [filter, setFilter] = useState('')
  const [query, setQuery] = useState('')            // 去抖后真正发给后端的筛选词
  const [question, setQuestion] = useState('')
  const [asked, setAsked] = useState('')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [importCategory, setImportCategory] = useState('')
  const [imported, setImported] = useState<ImportResult | null>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setQuery(filter.trim().replace(/\s+/g, ' ')), 250)
    return () => window.clearTimeout(id)
  }, [filter])

  const docs = useDocuments(base, query, category ?? '')
  const cats = useCategories(base)
  const search = useSearch(base)
  const add = useAddKnowledge(base)
  const upload = useUploadKnowledge(base)
  const taxonomy = cats.data?.categories ?? docs.data?.categories
  const label = (c: KnowledgeCategory) => categoryName(c, lang)

  const ask = (q: string) => {
    const text = q.trim()
    if (!text || search.isPending) return
    setQuestion(text)
    setAsked(text)
    search.mutate({ query: text })
  }

  /** 打开导入框。分类默认放在正在看的那一类里——在"安装与验收"下点导入，多半就是要导进这一类。 */
  const openImport = (preset?: string) => {
    add.reset(); upload.reset()
    const here = preset ?? category
    setImportCategory(here && taxonomy?.some((c) => c.key === here) ? here : '')
    dialogRef.current?.showModal()
    // 原生 dialog 打开时把焦点给第一个能聚焦的元素——这里是右上角的关闭按钮。
    // 打开导入是为了填东西：焦点直接落在标题上，键盘用户不用先绕过"关闭"
    titleRef.current?.focus()
  }
  const closeImport = () => dialogRef.current?.close()

  // 导入成功：关对话框、清筛选（新文档得露出来）、带到它所在的分类并报告结果
  const done = (result: ImportResult) => {
    closeImport()
    setTitle('')
    setContent('')
    setFilter('')
    setQuery('')
    setImported(result)
    navigate(landingFor(result))
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
    .map((d) => d.id).filter((id): id is string => !!id))
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
            <button className="btn btn-primary" onClick={() => openImport()}>
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

        {imported && (
          <ImportNotice result={imported} categories={taxonomy} onDismiss={() => setImported(null)} />
        )}

        {mode === 'browse' ? (
          <section aria-label={t('kb.tab.browse')} className="kb-browse-wrap">
            <Browse
              data={data} docs={docs} category={category} terms={terms} fresh={fresh} filter={filter}
              setFilter={setFilter} openImport={openImport}
              askInstead={() => { navigate(href.knowledge('ask')); ask(filter) }}
            />
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
                            {/* score 为 null 是 Embedding 不可用、只走了关键词一路：没有分数可显示，
                                但"这条是哪一路召回的"照样要说 */}
                            {item.score != null ? (
                              <p className="hit-source" title={t('kb.relevance.hint')}>
                                {t('kb.relevance')} <span className="mono">{num(item.score, 3)}</span>
                                {item.retrieval && <> · {item.retrieval}</>}
                                {search.data.reranked && <> · {t('kb.reranked')}</>}
                              </p>
                            ) : item.retrieval && (
                              <p className="hit-source">
                                {item.retrieval}
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
            {/* 分类对粘贴和上传都生效；老后端没有分类就不显示，导入照常 */}
            {taxonomy && (
              <div className="field-row">
                <label className="field-label" htmlFor="kb-import-category">{t('kb.cat.label')}</label>
                <select id="kb-import-category" className="field" value={importCategory}
                        aria-describedby="kb-import-category-hint"
                        onChange={(e) => setImportCategory(e.target.value)}>
                  <option value="">{t('kb.cat.none')}</option>
                  {taxonomy.map((c) => <option key={c.key} value={c.key}>{label(c)}</option>)}
                </select>
                <p className="field-hint" id="kb-import-category-hint">{t('kb.cat.hint')}</p>
              </div>
            )}
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
                onClick={() => add.mutate(
                  [{ title: title.trim(), content, ...(importCategory ? { category: importCategory } : {}) }],
                  { onSuccess: done },
                )}
              >
                {add.isPending ? t('kb.importing') : t('kb.doImport')}
              </button>
              <input
                ref={fileRef} type="file" accept=".txt,.md,.json" hidden
                aria-label={t('kb.upload')}
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) upload.mutate({ file, category: importCategory }, { onSuccess: done })
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

/**
 * 浏览文档：筛选框 + 分类目录 + 文档列表。
 *
 * 加载、库是空的、没匹配上、这一类是空的、地址里的分类不存在——这几种状态读起来
 * 得各不相同：把"这一类还没有文档"说成"没有匹配"，人会以为是筛选词的问题。
 */
function Browse({ data, docs, category, terms, fresh, filter, setFilter, openImport, askInstead }: {
  data: KnowledgeDocumentList | undefined
  docs: ReturnType<typeof useDocuments>
  category?: string
  terms: string[]
  fresh: Set<string>
  filter: string
  setFilter: (v: string) => void
  openImport: (preset?: string) => void
  askInstead: () => void
}) {
  const { t, lang } = useT()
  const facets = data?.categories                 // 没有 = 后端还不支持分类
  const loose = data?.uncategorized ?? 0
  const selected = facets ? category : undefined  // 老后端不认分类，地址里的就当没有
  const current = selected && selected !== UNCATEGORIZED ? facets?.find((c) => c.key === selected) : undefined
  const unknown = !!selected && selected !== UNCATEGORIZED && !current
  const label = (c: KnowledgeCategory) => categoryName(c, lang)
  const selectedName = selected === UNCATEGORIZED ? t('kb.cat.none') : current ? label(current) : selected ?? ''
  // 各类匹配篇数加起来 = 不分类时匹配的篇数（没有筛选词时就是全库）
  const matchedAll = facets ? facets.reduce((n, c) => n + c.documents, 0) + loose : data?.matched ?? 0

  const filterBar = (
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
  )

  const showAll = <a className="btn btn-secondary" href={href.knowledge('browse')}>{t('kb.cat.showAll')}</a>
  const nomatch = (
    <StateBlock title={t('kb.nomatch.title', { q: data?.query ?? '' })}
      action={<button className="btn btn-secondary" onClick={askInstead}>{t('kb.nomatch.ask')}</button>}>
      {t('kb.nomatch.body')}
    </StateBlock>
  )

  let body: ReactNode
  if (docs.isError) {
    const { body: text, detail } = explainError(docs.error, t, 'kb.error.404')
    body = (
      <StateBlock tone="danger" title={t('kb.error.title')} detail={detail}
        action={<button className="btn btn-secondary" onClick={() => docs.refetch()}>{t('kb.retry')}</button>}>
        {text}
      </StateBlock>
    )
  } else if (!data) {
    body = <StateBlock busy title={t('kb.loading')} />
  } else if (data.total_documents === 0) {
    body = (
      <StateBlock title={t('kb.empty.title')}
        action={<button className="btn btn-primary" onClick={() => openImport()}>{t('kb.import')}</button>}>
        {t('kb.empty.body')}
      </StateBlock>
    )
  } else if (unknown) {
    body = (
      <StateBlock title={t('kb.cat.unknown.title')} action={showAll}>
        {t('kb.cat.unknown.body', { key: selected ?? '' })}
      </StateBlock>
    )
  } else if (data.documents.length === 0) {
    if (!selected || (terms.length > 0 && matchedAll === 0)) {
      body = nomatch
    } else if (terms.length > 0) {
      // 这一类里没有，别的类里有：说清楚是分类把它们挡住了，不是筛选词不对
      body = (
        <StateBlock title={t('kb.cat.nomatch.title', { cat: selectedName, q: data.query })}
          action={<a className="btn btn-secondary" href={href.knowledge('browse')}>{t('kb.cat.nomatch.all')}</a>}>
          {t('kb.cat.nomatch.body', { n: matchedAll })}
        </StateBlock>
      )
    } else if (selected === UNCATEGORIZED) {
      body = <StateBlock title={t('kb.cat.loose.empty.title')} action={showAll}>{t('kb.cat.loose.empty.body')}</StateBlock>
    } else {
      body = (
        <StateBlock title={t('kb.cat.empty.title', { cat: selectedName })}
          action={<button className="btn btn-primary" onClick={() => openImport(selected)}>{t('kb.import')}</button>}>
          {t('kb.cat.empty.body')}
        </StateBlock>
      )
    }
  } else {
    const truncated = data.matched > data.documents.length && (
      <span> · {t('kb.list.truncated', { n: data.documents.length })}</span>
    )
    const summary = terms.length > 0 ? (
      <p className="list-summary" aria-live="polite">
        {selected
          ? t('kb.filter.summary.cat', { n: data.matched, cat: selectedName, q: data.query })
          : t('kb.filter.summary', { n: data.matched, total: data.total_documents, q: data.query })}
        {terms.length > 1 && <span> · {t('kb.filter.hint')}</span>}
        {truncated}
      </p>
    ) : selected ? (
      <div className="kb-list-title" aria-live="polite">
        <h3>{selectedName}</h3>
        <span className="kb-group-count">{t('kb.count.docs', { n: data.matched })}{truncated}</span>
      </div>
    ) : (!facets || truncated) && (
      <p className="list-summary" aria-live="polite">
        {t('kb.count.docs', { n: data.total_documents })}{truncated}
      </p>
    )
    body = (
      <>
        {summary}
        {selected === UNCATEGORIZED && <p className="kb-group-note">{t('kb.cat.loose.note')}</p>}
        {facets && !selected ? (
          <div className="kb-groups">
            {groupByCategory(data.documents, facets).map((g) => (
              <section key={g.key} className="kb-group" aria-labelledby={`kb-group-${g.key}`}>
                {/* 标题只是分类名，篇数跟在标题外面：读屏念的组名不带数字 */}
                <div className="kb-group-head">
                  <h3>
                    <a id={`kb-group-${g.key}`} href={href.category(g.key)}>
                      {g.category ? label(g.category) : t('kb.cat.none')}
                    </a>
                  </h3>
                  <span className="kb-group-count">{g.category ? g.category.documents : loose}</span>
                </div>
                {!g.category && <p className="kb-group-note">{t('kb.cat.loose.note')}</p>}
                <DocumentRows docs={g.docs} terms={terms} fresh={fresh} />
              </section>
            ))}
          </div>
        ) : (
          <DocumentRows docs={data.documents} terms={terms} fresh={fresh} />
        )}
      </>
    )
  }

  // 结构在加载前后保持不变：筛选框始终是同一个元素。要是加载完才换一套外层，
  // 加载时已经敲进去的字和焦点都会跟着旧的输入框一起丢掉。
  // 确认是老后端（有数据、没有分类）才收成一栏；加载中先按两栏占位，常见情况下不跳。
  const flat = !!data && !facets
  return (
    <div className={`kb-browse${flat ? ' is-flat' : ''}`}>
      {facets ? (
        <nav className="kb-cats" aria-label={t('kb.cat.nav')}>
          <CategoryLink to={href.knowledge('browse')} current={!selected} name={t('kb.cat.all')} n={matchedAll} />
          {facets.map((c) => (
            <CategoryLink key={c.key} to={href.category(c.key)} current={selected === c.key} name={label(c)} n={c.documents} />
          ))}
          {/* 未分类只在有的时候出现：它是一个"待处理"的筐，不是一个分类 */}
          {(loose > 0 || selected === UNCATEGORIZED) && (
            <CategoryLink to={href.category(UNCATEGORIZED)} current={selected === UNCATEGORIZED}
                          name={t('kb.cat.none')} n={loose} loose />
          )}
        </nav>
      ) : flat ? null : <div className="kb-cats" aria-hidden />}
      <div className="kb-docs" aria-busy={docs.isPlaceholderData || undefined}>
        {filterBar}
        {body}
      </div>
    </div>
  )
}

function CategoryLink({ to, current, name, n, loose }: {
  to: string; current: boolean; name: string; n: number; loose?: boolean
}) {
  return (
    <a href={to} aria-current={current ? 'page' : undefined}
       className={`kb-cat${n === 0 ? ' is-empty' : ''}${loose ? ' is-loose' : ''}`}>
      <span className="kb-cat-name">{name}</span>
      <span className="kb-cat-count">{n}</span>
    </a>
  )
}

function DocumentRows({ docs, terms, fresh }: { docs: KnowledgeDocument[]; terms: string[]; fresh: Set<string> }) {
  return (
    <ul className="doc-rows">
      {docs.map((doc) => <DocumentRow key={doc.id} doc={doc} terms={terms} isNew={fresh.has(doc.id)} />)}
    </ul>
  )
}

/**
 * 列表里的一篇：标题、状态标记、一行摘要。来源、导入时间、片段数放在阅读页——
 * 同一批导入的几十篇，这几项一模一样，每行重复一遍只会把标题挤散。
 * 筛选时摘要换成命中处的上下文（可以到两行），标题没命中而导入时的标题命中了，
 * 把导入时的标题也露出来，说清楚为什么匹配。
 */
function DocumentRow({ doc, terms, isNew }: { doc: KnowledgeDocument; terms: string[]; isNew: boolean }) {
  const { t } = useT()
  const origin = originLabel(doc)
  const name = doc.display_title || doc.title || t('kb.untitled')
  const snippet = doc.match?.snippet
  const summary = snippet || doc.excerpt
  return (
    <li className="doc-row">
      <div className="doc-row-head">
        {/* 链接只包标题，整行可点靠 ::after 铺满：读屏听到的链接名是文档标题 */}
        <a href={href.document(doc.id)}><HighlightTerms text={name} terms={terms} /></a>
        {isNew && <Chip tone="success">{t('kb.new')}</Chip>}
        {origin && <Chip tone="caution">{t(origin)}</Chip>}
        {doc.chunks === 0 && <Chip tone="danger">{t('kb.unindexed')}</Chip>}
      </div>
      {summary && (
        <p className={`doc-row-text${snippet ? ' is-match' : ''}`}><HighlightTerms text={summary} terms={terms} /></p>
      )}
      {terms.length > 0 && doc.title && doc.title !== name && (
        <p className="doc-row-meta"><HighlightTerms text={doc.title} terms={terms} /></p>
      )}
    </li>
  )
}

/**
 * 导入没成：先说该怎么办，后端的原话跟在后面。
 * 400 在这里只有一种意思——服务端读不了送过去的东西（JSON 解析失败、文件太大、
 * 格式不对、分类写错），所以单独给一句讲格式的话；其它状态码用通用的说明。
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

/** 导入结果：逐篇说落成了什么、在哪个分类，带着能直接打开的链接。 */
function ImportNotice({ result, categories, onDismiss }: {
  result: ImportResult; categories?: KnowledgeCategory[]; onDismiss: () => void
}) {
  const { t, lang } = useT()
  const documents: ImportedDocument[] = result.documents ?? []
  const where = (key?: string) => {
    if (key === undefined || !categories) return null
    const found = categories.find((c) => c.key === key)
    return t('kb.import.category', { cat: found ? categoryName(found, lang) : t('kb.cat.none') })
  }
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
              {doc.id && where(doc.category) && <> {where(doc.category)}</>}
              {doc.id && <> <a href={href.document(doc.id)}>{t('kb.import.open')}</a></>}
            </p>
          ))}
        {documents.length > 5 && <p>{t('kb.import.done.more', { n: documents.length - 5 })}</p>}
      </div>
      <button className="icon-btn" aria-label={t('kb.close')} onClick={onDismiss}><Icon name="close" size={16} /></button>
    </div>
  )
}
