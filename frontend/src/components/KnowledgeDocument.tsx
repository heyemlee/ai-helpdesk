import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useDocument } from '@/api/hooks'
import { Chip, StateBlock } from './ui'
import { Icon } from './Icon'
import { originLabel, sourceLabel } from '@/lib/knowledge'
import { dateTime } from '@/lib/format'
import { explainError } from '@/lib/errors'
import { href } from '@/lib/route'
import { useT } from '@/i18n'

/**
 * 一篇文档：先是能读的正文，检索用的分片放在折叠的次级详情里。
 *
 * 历史文档（加文档表之前导入的）正文是由分片拼回来的，这里照实标出来：
 * 齐全的说"重建"，缺片的说"不完整"。来源和文件名当时没记，就写"来源未记录"，
 * 不拿标题或导入时间去充当。
 */
export function KnowledgeDocumentView({ base, docId }: { base: string; docId: string }) {
  const { t, lang } = useT()
  const { data: doc, error, isError, refetch } = useDocument(base, docId)
  const back = (
    <a className="back-link" href={href.knowledge('browse')}>
      <Icon name="arrow" size={16} />{t('kb.doc.back')}
    </a>
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
