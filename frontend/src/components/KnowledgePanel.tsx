import { useEffect, useRef, useState } from 'react'
import { useAddKnowledge, useKnowledgeStats, useSearch, useUploadKnowledge } from '@/api/hooks'
import { Card, Chip, Empty, ErrorNote } from './ui'
import { Icon } from './Icon'
import { num } from '@/lib/format'

/** 中文功能词。不剔掉的话「什么时候」这类片段会把无关文本涂满黄底。 */
const STOP = /[的了吗呢吧和与及或是在有个能不什么怎样时候多少哪要请我你他她它们这那]/g

/**
 * 命中高亮。按「合并后的连续区间」涂色，不是逐个 n-gram 涂——
 * 后者会把「材料」拆成两块黄底、还可能正好跨行断开，高亮反而破坏阅读。
 */
function highlightParts(text: string, query: string): Array<{ t: string; hit: boolean }> {
  const core = query.replace(/[\s，。？?、,.!！]/g, '').replace(STOP, '')
  if (!core) return [{ t: text, hit: false }]

  const spans: Array<[number, number]> = []
  for (let n = Math.min(6, core.length); n >= 2; n--) {
    for (let i = 0; i + n <= core.length; i++) {
      const g = core.slice(i, i + n)
      let from = 0, at: number
      while ((at = text.indexOf(g, from)) !== -1) { spans.push([at, at + n]); from = at + 1 }
    }
  }
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
const SAMPLES = [
  '补件需要提交什么材料',
  '尾款什么时候开票',
  '已开料还能改吊柜尺寸吗',
  '门板缝隙不均能自己调吗',
]

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
    if (!text) return
    setQuery(text)
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
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="page-title">混合检索与重排</h2>
            <p className="page-lede">
              稠密向量与关键词双路并行检索，RRF 融合后再过一次重排模型。
              这里检索到什么，对话工作台里 RAG 就引用什么。
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            {/* 拿不到统计就说拿不到，不拿占位符冒充数据 */}
            {stats.isError
              ? <Chip tone="danger" dot title={stats.error instanceof Error ? stats.error.message : undefined}>
                  统计不可用
                </Chip>
              : stats.data
                ? <Chip mono>{stats.data.total_chunks} 片段</Chip>
                : <Chip>统计加载中…</Chip>}
            <button className="btn btn-primary" onClick={openImport}>
              <Icon name="upload" size={16} />导入文档
            </button>
          </div>
        </div>

        <div className="searchbar">
          <Icon name="search" />
          <label className="sr-only" htmlFor="kb-q">检索知识库</label>
          <input
            id="kb-q" value={query} autoFocus
            placeholder="检索知识库"
            onChange={(e) => setQuery(e.target.value)}
            // 同 ChatPanel：组合态的 Enter 是上屏确认，不是检索
            onKeyDown={(e) => !e.nativeEvent.isComposing && e.key === 'Enter' && runSearch(query)}
          />
          {search.isPending
            ? <span className="hint">检索中…</span>
            : query.trim() && <span className="hint">Enter 检索</span>}
        </div>

        <ErrorNote error={search.error} />

        {/* 还没搜过：给邀请，不给空白。空白会被读成"搜过了没结果"。 */}
        {!searched && !search.error && (
          <section>
            <p className="section-label">试试这些</p>
            <div className="suggestions">
              {SAMPLES.map((s) => (
                <button key={s} className="suggestion" onClick={() => runSearch(s)}>{s}</button>
              ))}
            </div>
          </section>
        )}

        {search.isSuccess && (
          <section>
            <div className="chip-row" style={{ marginBottom: 12 }}>
              <span className="section-label" style={{ margin: 0 }}>
                {results.length} 条命中
              </span>
              {search.data?.reranked && <Chip tone="info">已重排</Chip>}
            </div>
            {results.length === 0
              ? <Empty>没有命中结果。换个说法，或把对应文档导入知识库。</Empty>
              : (
                <div className="dl">
                  {results.map((item, i) => (
                    <article className="hit" key={i}>
                      <div className="hit-body">
                        <div className="hit-head">
                          <strong>{item.title || '未命名'}</strong>
                          {item.fallback && <Chip tone="caution">降级结果</Chip>}
                          {item.score !== undefined && (
                            <span className="hit-score">
                              相关度 <span className="mono">{num(item.score, 3)}</span>
                            </span>
                          )}
                        </div>
                        <p className="hit-text">
                          {highlightParts(item.content ?? '', query).map((p, j) =>
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

        <dialog className="modal" ref={dialogRef} onClose={closeImport}>
          <Card
            title="导入文档"
            actions={
              <button className="icon-btn" aria-label="关闭" onClick={closeImport}>
                <Icon name="close" />
              </button>
            }
            bodyStyle={{ display: 'grid', gap: 12 }}
          >
            <label className="field-row">
              <span>标题</span>
              <input className="field" value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder="例如：补件材料清单（2026 版）" />
            </label>
            <label className="field-row">
              <span>内容</span>
              <textarea className="field" rows={5} value={content} onChange={(e) => setContent(e.target.value)}
                        placeholder="正文。导入后会触发一次增量向量化。" />
            </label>
            <ErrorNote error={add.error ?? upload.error} />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button className="btn btn-ghost" disabled={upload.isPending} aria-busy={upload.isPending}
                      onClick={() => fileRef.current?.click()}>
                <Icon name="upload" size={16} />
                {upload.isPending ? '上传中…' : '上传文件'}
              </button>
              <span className="spacer" />
              <button className="btn btn-secondary" onClick={closeImport}>取消</button>
              <button
                className="btn btn-primary" aria-busy={add.isPending}
                disabled={add.isPending || !title.trim() || !content.trim()}
                onClick={() => add.mutate([{ title: title.trim(), content: content.trim() }])}
              >
                {add.isPending ? '导入中…' : '导入'}
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
