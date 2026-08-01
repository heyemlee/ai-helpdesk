import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { CATALOG, type Key } from './catalog'

const STORAGE_KEY = 'helpdesk.lang'
export type Lang = 'en' | 'zh'
const FALLBACK: Lang = 'en'

/**
 * 默认英语，不跟随浏览器语言——和原型页（assets/i18n.js）保持同一口径，
 * 两边共用 localStorage 的 helpdesk.lang，来回跳转时语言不会跳变。
 *
 * 后端返回的中文分两类：
 *   · 闭集（Agent 名 / 策略规则标签）在 catalog 里映射；
 *   · 自由文本（回复正文、routing.reason、知识库片段）原样显示——
 *     前端翻不了，翻了就是伪造后端行为。
 */
/* localStorage 在某些环境里不可用（jsdom 没有 origin 时 window.localStorage 就是
   undefined，测试环境正是如此）。语言偏好只是锦上添花，读写失败不该让界面挂掉。 */
function readStored(): Lang | null {
  try {
    const saved = globalThis.localStorage?.getItem(STORAGE_KEY)
    return saved === 'zh' || saved === 'en' ? saved : null
  } catch { return null }
}

function writeStored(lang: Lang) {
  try { globalThis.localStorage?.setItem(STORAGE_KEY, lang) } catch { /* 存不下就算了 */ }
}

type Ctx = {
  lang: Lang
  setLang: (l: Lang) => void
  toggle: () => void
  /** 缺键时返回键名本身：宁可屏幕上出现刺眼的 key，也不要静默空白。 */
  t: (key: Key, vars?: Record<string, string | number>) => string
}

const LangCtx = createContext<Ctx | null>(null)

export function LangProvider({ children, initial }: {
  children: ReactNode
  /** 仅供测试与预览固定语言用；正常运行时读存储、缺省英语。 */
  initial?: Lang
}) {
  const [lang, setLangState] = useState<Lang>(() => initial ?? readStored() ?? FALLBACK)

  useEffect(() => {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
  }, [lang])

  const setLang = useCallback((next: Lang) => {
    writeStored(next)
    setLangState(next)
  }, [])

  const value = useMemo<Ctx>(() => ({
    lang,
    setLang,
    toggle: () => setLang(lang === 'zh' ? 'en' : 'zh'),
    t: (key, vars) => {
      const table = CATALOG[lang] as Record<string, string>
      const text = table[key] ?? (CATALOG[FALLBACK] as Record<string, string>)[key] ?? key
      // `单数|复数`：按 n 选一段（见 catalog.ts 顶部的约定）
      const forms = text.split('|')
      const raw = forms.length === 2 && typeof vars?.n === 'number'
        ? forms[vars.n === 1 ? 0 : 1]!
        : text
      return vars
        ? raw.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m))
        : raw
    },
  }), [lang, setLang])

  return <LangCtx.Provider value={value}>{children}</LangCtx.Provider>
}

export function useT() {
  const ctx = useContext(LangCtx)
  if (!ctx) throw new Error('useT 必须在 <LangProvider> 内使用')
  return ctx
}

export type { Key }
