/**
 * 对话回答的排版（移植自旧控制台的 richtext.js）。
 *
 * 对话路径的回答是模型写的一段文字，里面常带 Markdown 记号（**加粗**、`代码`、列表）。原样显示满屏
 * 星号，直接当 HTML 插入又等于让模型往页面里写标记。这里只认最常见的几种记号，其余一律当普通文字；
 * 结果是一棵纯数据的树，由组件按文本渲染（React 默认转义），不解析链接、图片和原始 HTML——这段文字
 * 是解释，不需要能点的东西。
 */

export type Inline = { kind: 'text' | 'strong' | 'code'; text: string }

export type RichBlock =
  | { kind: 'paragraph'; lines: Inline[][] }
  | { kind: 'heading'; content: Inline[] }
  | { kind: 'list'; ordered: boolean; items: Inline[][] }

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g

export function parseInline(text: string): Inline[] {
  return text.split(INLINE).filter((part) => part !== '').map((part): Inline => {
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) return { kind: 'strong', text: part.slice(2, -2) }
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) return { kind: 'code', text: part.slice(1, -1) }
    return { kind: 'text', text: part }
  })
}

export function parseRichText(text: string | null | undefined): RichBlock[] {
  const blocks: RichBlock[] = []
  let paragraph: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null

  const endParagraph = () => {
    if (!paragraph.length) return
    blocks.push({ kind: 'paragraph', lines: paragraph.map(parseInline) })
    paragraph = []
  }
  const endList = () => {
    if (!list) return
    blocks.push({ kind: 'list', ordered: list.ordered, items: list.items.map(parseInline) })
    list = null
  }

  for (const raw of String(text ?? '').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line) { endParagraph(); endList(); continue }

    const bullet = /^[-*•]\s+(.*)$/.exec(line)
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      endParagraph()
      const ordered = Boolean(numbered)
      if (list && list.ordered !== ordered) endList()
      if (!list) list = { ordered, items: [] }
      list.items.push((bullet ?? numbered)![1]!)
      continue
    }

    endList()
    const heading = /^#{1,6}\s+(.*)$/.exec(line)
    if (heading) {
      endParagraph()
      blocks.push({ kind: 'heading', content: parseInline(heading[1]!) })
      continue
    }
    paragraph.push(line)
  }
  endParagraph()
  endList()
  return blocks
}
