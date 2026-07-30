import { useEffect, useRef, useState } from 'react'
import { useAddKnowledge, useKnowledgeStats, useSearch, useUploadKnowledge } from '@/api/hooks'
import { Card, Chip, Empty, ErrorNote } from './ui'
import { Icon } from './Icon'
import { num } from '@/lib/format'
import { useT } from '@/i18n'

/** 中文功能词。不剔掉的话「什么时候」这类片段会把无关文本涂满黄底。 */
const STOP_ZH = /[的了吗呢吧和与及或是在有个能不什么怎样时候多少哪要请我你他她它们这那]/g

/** 英文功能词。中文那套按字切，英文必须按词切——把空格剥掉再做字符 n-gram，
 *  "what documents for a part" 会变成一整串，高亮出来全是噪声。 */
const STOP_EN = new Set(['the', 'a', 'an', 'of', 'to', 'for', 'in', 'on', 'at', 'is', 'are', 'do',
  'does', 'did', 'i', 'we', 'you', 'it', 'my', 'our', 'can', 'could', 'will', 'would', 'how',
  'what', 'when', 'where', 'why', 'and', 'or', 'if', 'be', 'with', 'about', 'need', 'needs', 'after'])

/**
 * 命中高亮。按「合并后的连续区间」涂色，不是逐个 n-gram 涂——
 * 后者会把「材料」拆成两块黄底、还可能正好跨行断开，高亮反而破坏阅读。
 *
 * 按**查询本身**的语种分路，不是按界面语言：界面开着英文也可能搜中文。
 */
function highlightParts(text: string, query: string): Array<{ t: string; hit: boolean }> {
  const spans = /[\u4e00-\u9fff]/.test(query)
    ? spansZh(text, query)
    : spansEn(text, query)
  return toParts(text, spans)
}

function spansZh(text: string, query: string): Array<[number, number]> {
  const core = query.replace(/[\s，。？?、,.!！]/g, '').replace(STOP_ZH, '')
  const spans: Array<[number, number]> = []
  if (!core) return spans
  for (let n = Math.min(6, core.length); n >= 2; n--) {
    for (let i = 0; i + n <= core.length; i++) {
      const g = core.slice(i, i + n)
      let from = 0, at: number
      while ((at = text.indexOf(g, from)) !== -1) { spans.push([at, at + n]); from = at + 1 }
    }
  }
  return spans
}

function spansEn(text: string, query: string): Array<[number, number]> {
  const words = query.toLowerCase().split(/[^a-z0-9]+/)
    .filter((w) => w.length > 2 && !STOP_EN.has(w))
  const spans: Array<[number, number]> = []
  if (!words.length) return spans
  const lower = text.toLowerCase()
  for (const w of words) {
    // 后缀归并：复数/时态不该漏掉命中，但只在词首匹配，避免 "the" 命中 "other"
    const stem = w.replace(/(ies|es|s|ing|ed)$/, '')
    if (stem.length < 3) continue
    const re = new RegExp(`\\b${stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[a-z]*`, 'g')
    let m: RegExpExecArray | null
    while ((m = re.exec(lower)) !== null) spans.push([m.index, m.index + m[0].length])
  }
  return spans
}

function toParts(text: string, spans: Array<[number, number]>): Array<{ t: string; hit: boolean }> {
  if (!spans.length) return [{ t: text, hit: false }]

  spans.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = [spans[0]!]
  for (let i = 1; i < spans.length; i++) {
    const last = merged[merged.length - 1]!
    const cur = spans[i]!
    if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1])
    else merged.push(cur)
  }

  const parts: Array<{ t: string; hit: boolean }> = []
  let cursor = 0
  for (const [a, b] of merged) {
    if (a > cursor) parts.push({ t: text.slice(cursor, a), hit: false })
    parts.push({ t: text.slice(a, b), hit: true })
    cursor = b
  }
  if (cursor < text.length) parts.push({ t: text.slice(cursor), hit: false })
  return parts
}

/** 空态的邀请。用真实的橱柜域问法，点一下就搜——比一个空白页面有用得多。 */
const SAMPLE_KEYS = ['kb.sample.1', 'kb.sample.2', 'kb.sample.3', 'kb.sample.4'] as const

/**
 * 知识库：稠密向量 + 关键词双路混合检索，RRF 融合后再过一次重排。
 * 这里检索到什么，对话工作台里 RAG 就引用什么——排查「回复没有出处」先从这一页开始。
 *
 * 交互姿态是「检索工具」而不是「仪表盘」：
 *   - 落地不预填查询。预填了却不检索，等于让用户看见一个假的失败态。
 *   - 搜索栏独立成页面第一元素，Enter 即搜；实心主按钮留给「导入文档」这种真正的创建动作。
 *   - 导入走模态，不把检索结果推走；导入成功后自动检索新片段，让用户看见它确实进去了。
 */
export function KnowledgePanel({ base }: { base: string }) {
  const { t } = useT()
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [query, setQuery] = useState('')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const dialogRef = useRef<HTMLDialogElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const stats = useKnowledgeStats(base)
  const search = useSearch(base)
  const add = useAddKnowledge(base)
  const upload = useUploadKnowledge(base)

  const runSearch = (q: string) => {
    const text = q.trim()
    if (!text || search.isPending) return
    setQuery(text)
    setSubmittedQuery(text)
    search.mutate({ query: text })
  }

  const openImport = () => dialogRef.current?.showModal()
  const closeImport = () => dialogRef.current?.close()

  // 导入成功 → 关模态、清字段、用新标题检索一次，让"进去了没有"当场有答案
  const imported = add.data ?? upload.data
  useEffect(() => {
    if (!imported) return
    closeImport()
    const justAdded = title.trim()
    setTitle('')
    setContent('')
    if (justAdded) runSearch(justAdded)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imported])

  const results = search.data?.results ?? []
  const searched = search.isSuccess || search.isPending

  return (
    <div className="page scroll">
      <div className="page-inner">
        <div className="page-heading">
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="page-title">{t('kb.pageTitle')}</h2>
            <p className="page-lede">
              {t('kb.lede')}
            </p>
          </div>
          <div className="page-heading-actions">
            {/* 拿不到统计就说拿不到，不拿占位符冒充数据 */}
            {stats.isError
              ? <Chip tone="danger" dot title={stats.error instanceof Error ? stats.error.message : undefined}>
                  {t('kb.statsDown')}
                </Chip>
              : stats.data
                ? <Chip mono>{stats.data.total_chunks} {t('kb.chunks')}</Chip>
                : <Chip>{t('kb.statsLoading')}</Chip>}
            <button className="btn btn-primary" onClick={openImport}>
              <Icon name="upload" size={16} />{t('kb.import')}
            </button>
          </div>
        </div>

        <form className="searchbar" onSubmit={(e) => { e.preventDefault(); runSearch(query) }}>
          <Icon name="search" />
          <label className="sr-only" htmlFor="kb-q">{t('kb.searchLabel')}</label>
          <input
            id="kb-q" value={query}
            placeholder={t('kb.searchLabel')}
            onChange={(e) => setQuery(e.target.value)}
            // 同 ChatPanel：组合态的 Enter 是上屏确认，不是检索
            onKeyDown={(e) => { if (e.nativeEvent.isComposing && e.key === 'Enter') e.preventDefault() }}
          />
          <button className="btn btn-primary" type="submit" disabled={search.isPending || !query.trim()} aria-busy={search.isPending}>
            {search.isPending ? t('kb.searching') : t('kb.search')}
          </button>
        </form>

        <ErrorNote error={search.error} />

        {/* 还没搜过：给邀请，不给空白。空白会被读成"搜过了没结果"。 */}
        {!searched && !search.error && (
          <section className="knowledge-welcome">
            <div className="welcome-mark"><Icon name="book" size={28} /></div>
            <h3>{t('kb.welcome')}</h3>
            <p>{t('kb.welcome.body')}</p>
            <p className="section-label">{t('kb.try')}</p>
            <div className="suggestions">
              {SAMPLE_KEYS.map((k) => (
                <button key={k} className="suggestion" onClick={() => runSearch(t(k))}>{t(k)}</button>
              ))}
            </div>
          </section>
        )}

        {search.isSuccess && (
          <section>
            <div className="chip-row" style={{ marginBottom: 12 }}>
              <span className="section-label" style={{ margin: 0 }}>
                {results.length} {t('kb.hitsN')}
              </span>
              {search.data?.reranked && <Chip tone="info">{t('kb.reranked')}</Chip>}
            </div>
            {results.length === 0
              ? <Empty>{t('kb.noResults')}</Empty>
              : (
                <div className="dl">
                  {results.map((item, i) => (
                    <article className="hit" key={i}>
                      <div className="hit-body">
                        <div className="hit-head">
                          <strong>{item.title || t('kb.untitled')}</strong>
                          {item.fallback && <Chip tone="caution">{t('kb.degraded')}</Chip>}
                          {item.score !== undefined && (
                            <span className="hit-score" title={t('kb.relevance.hint')}>
                              {t('kb.relevance')} <span className="mono">{num(item.score, 3)}</span>
                            </span>
                          )}
                        </div>
                        <p className="hit-text">
                          {highlightParts(item.content ?? '', submittedQuery).map((p, j) =>
                            p.hit ? <mark key={j}>{p.t}</mark> : <span key={j}>{p.t}</span>,
                          )}
                        </p>
                      </div>
                    </article>
                  ))}
                </div>
              )}
          </section>
        )}

        <dialog className="modal" ref={dialogRef} aria-label={t('kb.import')}>
          <Card
            title={t('kb.import')}
            actions={
              <button className="icon-btn" aria-label={t('kb.close')} onClick={closeImport}>
                <Icon name="close" />
              </button>
            }
            bodyStyle={{ display: 'grid', gap: 12 }}
          >
            <label className="field-row">
              <span>{t('kb.fTitle')}</span>
              <input className="field" value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder={t('kb.fTitlePh')} />
            </label>
            <label className="field-row">
              <span>{t('kb.fBody')}</span>
              <textarea className="field" rows={5} value={content} onChange={(e) => setContent(e.target.value)}
                        placeholder={t('kb.fBodyPh')} />
            </label>
            <ErrorNote error={add.error ?? upload.error} />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
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
                onClick={() => add.mutate([{ title: title.trim(), content: content.trim() }])}
              >
                {add.isPending ? t('kb.importing') : t('kb.doImport')}
              </button>
              <input
                ref={fileRef} type="file" accept=".txt,.md,.json" hidden
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) upload.mutate(file)
                }}
              />
            </div>
          </Card>
        </dialog>
      </div>
    </div>
  )
}
