/** 命中高亮。两种口径：按问题（语义检索的结果）和按字面词（文档筛选的结果）。 */

type Part = { t: string; hit: boolean }

/** 中文功能词。不剔掉的话「什么时候」这类片段会把无关文本涂满底色。 */
const STOP_ZH = /[的了吗呢吧和与及或是在有个能不什么怎样时候多少哪要请我你他她它们这那]/g

/** 英文功能词。中文那套按字切，英文必须按词切——把空格剥掉再做字符 n-gram，
 *  "what documents for a part" 会变成一整串，高亮出来全是噪声。 */
const STOP_EN = new Set(['the', 'a', 'an', 'of', 'to', 'for', 'in', 'on', 'at', 'is', 'are', 'do',
  'does', 'did', 'i', 'we', 'you', 'it', 'my', 'our', 'can', 'could', 'will', 'would', 'how',
  'what', 'when', 'where', 'why', 'and', 'or', 'if', 'be', 'with', 'about', 'need', 'needs', 'after'])

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

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
    const re = new RegExp(`\\b${escapeRe(stem)}[a-z]*`, 'g')
    let m: RegExpExecArray | null
    while ((m = re.exec(lower)) !== null) spans.push([m.index, m.index + m[0].length])
  }
  return spans
}

/** 按「合并后的连续区间」切，不是逐个命中切——后者会把相邻命中拆成碎块。 */
function toParts(text: string, spans: Array<[number, number]>): Part[] {
  if (!spans.length) return [{ t: text, hit: false }]

  spans.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = [spans[0]!]
  for (let i = 1; i < spans.length; i++) {
    const last = merged[merged.length - 1]!
    const cur = spans[i]!
    if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1])
    else merged.push(cur)
  }

  const parts: Part[] = []
  let cursor = 0
  for (const [a, b] of merged) {
    if (a > cursor) parts.push({ t: text.slice(cursor, a), hit: false })
    parts.push({ t: text.slice(a, b), hit: true })
    cursor = b
  }
  if (cursor < text.length) parts.push({ t: text.slice(cursor), hit: false })
  return parts
}

/**
 * 按问题高亮：检索是语义的，所以这里只是帮人扫读，标出和问题里的词相关的地方。
 * 按**查询本身**的语种分路，不是按界面语言：界面开着英文也可能搜中文。
 */
function questionParts(text: string, query: string): Part[] {
  const spans = /[一-鿿]/.test(query) ? spansZh(text, query) : spansEn(text, query)
  return toParts(text, spans)
}

/** 按字面词高亮：文档筛选是「每个词都要原样出现」，高亮的就是那些词本身。 */
function termParts(text: string, terms: string[]): Part[] {
  const lower = text.toLowerCase()
  const spans: Array<[number, number]> = []
  for (const term of terms) {
    const needle = term.toLowerCase()
    if (!needle) continue
    let from = 0, at: number
    while ((at = lower.indexOf(needle, from)) !== -1) {
      spans.push([at, at + needle.length])
      from = at + needle.length
    }
  }
  return toParts(text, spans)
}

const render = (parts: Part[]) =>
  parts.map((p, i) => (p.hit ? <mark key={i}>{p.t}</mark> : <span key={i}>{p.t}</span>))

export const HighlightQuestion = ({ text, query }: { text: string; query: string }) =>
  <>{render(questionParts(text, query))}</>

export const HighlightTerms = ({ text, terms }: { text: string; terms: string[] }) =>
  <>{render(termParts(text, terms))}</>
