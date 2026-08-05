import type { Key } from '@/i18n/catalog'
import type { Lang } from '@/i18n'
import type { KnowledgeCategory, KnowledgeDocument } from '@/api/types'

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

/** 按"未分类"浏览时地址里用的 key；和后端约定的保留字一致，它不会是哪个大类的 key。 */
export const UNCATEGORIZED = 'uncategorized'

/** 分类名跟着界面语言走。名字由后端给，界面里不写死任何一个分类。 */
export function categoryName(category: Pick<KnowledgeCategory, 'name' | 'name_en'>, lang: Lang): string {
  return lang === 'zh' ? category.name : category.name_en || category.name
}

/**
 * 把文档按分类分组，组的顺序就是 categories 的顺序（后端按客户流程排好的），
 * 最后是未分类。没有文档的组不出现。不认识的分类按未分类算，不让文档凭空消失。
 */
export function groupByCategory<D extends Pick<KnowledgeDocument, 'category'>>(
  docs: D[], categories: KnowledgeCategory[],
): Array<{ key: string; category: KnowledgeCategory | null; docs: D[] }> {
  const groups = new Map(categories.map((c) => [c.key, { key: c.key, category: c as KnowledgeCategory | null, docs: [] as D[] }]))
  const loose = { key: UNCATEGORIZED, category: null, docs: [] as D[] }
  for (const doc of docs) (groups.get(doc.category ?? '') ?? loose).docs.push(doc)
  return [...groups.values(), loose].filter((g) => g.docs.length > 0)
}
