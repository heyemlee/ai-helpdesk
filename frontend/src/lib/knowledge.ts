import type { Key } from '@/i18n/catalog'
import type { KnowledgeDocument } from '@/api/types'

const SOURCE_KEYS: Record<string, Key> = {
  manual: 'kb.source.manual',
  upload: 'kb.source.upload',
  seed: 'kb.source.seed',
  legacy: 'kb.source.legacy',
}

/** 来源只说记下来的：上传的带文件名，历史导入的照实说"没记"。 */
export function sourceLabel(
  doc: Pick<KnowledgeDocument, 'source_type' | 'source_name'>, t: (k: Key) => string,
): string {
  const label = t(SOURCE_KEYS[doc.source_type] ?? 'kb.source.legacy')
  return doc.source_name ? `${label} · ${doc.source_name}` : label
}

/** 历史文档的标记：单片的说"历史导入"，多片拼回的说"重建"，缺片的说"不完整"。 */
export function originLabel(doc: Pick<KnowledgeDocument, 'origin' | 'chunk_count'>): Key | null {
  if (doc.origin === 'legacy_partial') return 'kb.origin.partial'
  if (doc.origin === 'legacy') return doc.chunk_count > 1 ? 'kb.origin.rebuilt' : 'kb.origin.legacy'
  return null
}
