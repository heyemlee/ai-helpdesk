import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useCategories, useDocument, useSetDocumentCategory } from '@/api/hooks'
import { Chip, StateBlock } from './ui'
import { Icon } from './Icon'
import { UNCATEGORIZED, categoryName, originLabel, sourceLabel } from '@/lib/knowledge'
import { dateTime } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { href } from '@/lib/route'
import { useT } from '@/i18n'
import type { KnowledgeCategory } from '@/api/types'

/**
 * 一篇文档：先是能读的正文，检索用的分片放在折叠的次级详情里。
 *
 * 历史文档（加文档表之前导入的）正文是由分片拼回来的，这里照实标出来：
 * 齐全的说"重建"，缺片的说"不完整"。来源和文件名当时没记，就写"来源未记录"，
 * 不拿标题或导入时间去充当。
 *
 * 页头的位置是"全部文档 / 所在分类"，两段都能点回去；分类可以就地改，
 * 改的只是分类——正文、片段和检索都不动。后端还没有分类时只留"全部文档"。
 */
export function KnowledgeDocumentView({ base, docId }: { base: string; docId: string }) {
  const { t, lang } = useT()
  const { data: doc, error, isError, refetch } = useDocument(base, docId)
  const categories = useCategories(base).data?.categories
  const filed = doc?.category !== undefined && categories ? doc.category ?? '' : null
  const here = filed === null ? null : categories?.find((c) => c.key === filed)
  const back = (
    <nav className="crumbs" aria-label={t('kb.doc.crumbs')}>
      <a className="back-link" href={href.knowledge('browse')}>
        <Icon name="arrow" size={16} />{t('kb.doc.back')}
      </a>
      {filed !== null && (
        <>
          <span className="crumbs-sep" aria-hidden>/</span>
          <a href={href.category(filed || UNCATEGORIZED)}>
            {here ? categoryName(here, lang) : t('kb.cat.none')}
          </a>
        </>
      )}
    </nav>
  )

  if (isError) {
    const { body, detail } = explainError(error, t, 'kb.doc.missing')
    return (
      <div className="page scroll"><div className="page-inner reader">
        {back}
        <StateBlock tone="danger" title={t('kb.doc.error')} detail={detail}
          action={<button className="btn btn-secondary" onClick={() => refetch()}>{t('kb.retry')}</button>}>
          {body}
        </StateBlock>
      </div></div>
    )
  }
  if (!doc) {
    return (
      <div className="page scroll"><div className="page-inner reader">
        {back}
        <StateBlock busy title={t('kb.doc.loading')} />
      </div></div>
    )
  }

  const origin = originLabel(doc)
  const name = doc.display_title || doc.title || t('kb.untitled')
  const imported = dateTime(doc.created_at, lang)
  // body 是后端去掉开头标题行之后的正文：标题已经在页头显示过了，不再重复一遍
  const body = doc.body ?? doc.content

  return (
    <div className="page scroll">
      <article className="page-inner reader">
        {back}
        <header className="reader-head">
          <h2 className="page-title">{name}</h2>
          <dl className="reader-meta">
            <div><dt>{t('kb.meta.source')}</dt><dd>{sourceLabel(doc, t)}</dd></div>
            {imported && <div><dt>{t('kb.meta.importedLabel')}</dt><dd>{imported}</dd></div>}
            {doc.title && doc.title !== name && (
              <div><dt>{t('kb.meta.titleLabel')}</dt><dd>{doc.title}</dd></div>
            )}
            <div><dt>{t('kb.meta.size')}</dt><dd>{t('kb.count.passages', { n: doc.chunks })}</dd></div>
            {filed !== null && categories && (
              <CategoryField base={base} docId={doc.id} value={filed} categories={categories} />
            )}
          </dl>
          {origin && <Chip tone="caution">{t(origin)}</Chip>}
        </header>

        {doc.origin === 'legacy' && (
          <p className="note caution">
            {doc.chunk_count > 1
              ? t('kb.origin.note.rebuilt', { n: doc.chunk_count })
              : t('kb.origin.note.single')}
          </p>
        )}
        {doc.origin === 'legacy_partial' && (
          <p className="note caution">
            {t('kb.origin.note.partial', { have: doc.chunks, total: doc.chunk_count })}
          </p>
        )}
        {doc.chunks === 0 && <p className="note danger">{t('kb.doc.unindexed')}</p>}

        <div className="prose-chat reader-body">
          <Markdown remarkPlugins={[remarkGfm]}>{body}</Markdown>
        </div>

        <details className="raw reader-tech">
          <summary>{t('kb.doc.tech')}</summary>
          <p className="reader-tech-lede">{t('kb.doc.tech.body')}</p>
          <dl className="reader-ids">
            <div><dt>{t('kb.doc.id')}</dt><dd className="mono">{doc.id}</dd></div>
          </dl>
          <ol className="passage-list">
            {doc.passages.map((p) => (
              <li key={p.id}>
                <p className="passage-head">
                  <strong>{t('kb.passage', { n: p.index + 1, total: doc.chunk_count })}</strong>
                  <span>{t('kb.passage.chars', { n: p.chars })}</span>
                  <span>{t(p.indexed ? 'kb.passage.indexed' : 'kb.passage.unindexed')}</span>
                  <span className="mono">{p.id}</span>
                </p>
                <p className="passage-text">{p.content}</p>
              </li>
            ))}
          </ol>
        </details>
      </article>
    </div>
  )
}

/**
 * 阅读页里的分类：一个下拉框，选了就存。存的时候下拉框先显示新值，失败了退回原值并说一句。
 */
function CategoryField({ base, docId, value, categories }: {
  base: string; docId: string; value: string; categories: KnowledgeCategory[]
}) {
  const { t, lang } = useT()
  const move = useSetDocumentCategory(base, docId)
  const shown = move.isPending ? move.variables ?? value : value
  return (
    <div>
      <dt><label htmlFor="doc-category">{t('kb.cat.label')}</label></dt>
      <dd className="category-field">
        <select id="doc-category" className="field field-compact" value={shown} title={t('kb.cat.hint')}
                disabled={move.isPending} aria-describedby="doc-category-status"
                onChange={(e) => move.mutate(e.target.value)}>
          <option value="">{t('kb.cat.none')}</option>
          {categories.map((c) => <option key={c.key} value={c.key}>{categoryName(c, lang)}</option>)}
        </select>
        <span id="doc-category-status" className="category-status" role="status">
          {move.isPending ? t('kb.cat.saving') : move.isError ? null : move.isSuccess ? t('kb.cat.saved') : null}
        </span>
        {move.isError && (
          <span className="category-error" role="alert">
            {t('kb.cat.error')} <span className="mono">{explainError(move.error, t).detail}</span>
          </span>
        )}
      </dd>
    </div>
  )
}
