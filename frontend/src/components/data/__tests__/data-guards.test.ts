// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { EN, ZH } from '@/i18n/catalog'
import catalog from '@/test/fixtures/sql/catalog.json'

/**
 * 旧 SQL 控制台的几条整页检查（ai-sql-agent/tests/test_console_frontend.py），在新控制台里照样做一份：
 * 中英文案对齐、服务端给的文字只按文本渲染、页面里不写死业务数据、颜色只在样式表里。
 */
const SRC = path.join(process.cwd(), 'src')
const DATA_SOURCES = ['components/data', 'lib/sql', 'api/sql'].map((dir) => path.join(SRC, dir))

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) return name === '__tests__' ? [] : walk(full)
    return /\.(ts|tsx)$/.test(name) && !name.endsWith('.test.ts') && !name.endsWith('.test.tsx') ? [full] : []
  })
}

const sources = DATA_SOURCES.flatMap(walk).filter((file) => !file.endsWith('generated.ts'))
const rel = (file: string) => path.relative(SRC, file)

describe('数据页面的整页检查', () => {
  it('sq. 文案两种语言都有、都不是空的，占位符一一对应', () => {
    const en = EN as Record<string, string>
    const zh = ZH as Record<string, string>
    const keys = Object.keys(en).filter((key) => key.startsWith('sq.'))
    expect(keys.length).toBeGreaterThan(100)
    for (const key of keys) {
      expect(zh[key], key).toBeTruthy()
      expect(en[key]!.trim(), key).not.toBe('')
      const vars = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      expect(vars(zh[key]!), key).toEqual([...new Set(vars(en[key]!))])
    }
    expect(Object.keys(zh).filter((key) => key.startsWith('sq.')).sort()).toEqual(keys.sort())
  })

  it('服务端给的文字（问题、SQL、报错、数据）只按文本渲染：数据页面里没有 innerHTML', () => {
    for (const file of sources) {
      const text = readFileSync(file, 'utf8')
      expect(text, rel(file)).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML|document\.write/)
    }
  })

  it('页面里没有写死的库名、表名、字段名：数据只来自服务端的目录', () => {
    const names = new Set<string>()
    for (const domain of catalog.domains) {
      names.add(domain.database)
      for (const table of domain.tables) {
        names.add(table.name)
        for (const column of table.columns) if (column.name.includes('_')) names.add(column.name)
      }
    }
    for (const file of sources) {
      const text = readFileSync(file, 'utf8')
      const literals = [...text.matchAll(/(['"`])([A-Za-z_][\w]*)\1/g)].map((m) => m[2])
      const hardcoded = literals.filter((literal) => names.has(literal!))
      expect(hardcoded, rel(file)).toEqual([])
    }
  })

  it('颜色只在样式表里，分类色就是校验过的那六个、按那个顺序', () => {
    const css = readFileSync(path.join(SRC, 'components', 'data', 'data.css'), 'utf8')
    const series = [...css.matchAll(/--series-(\d):\s*(#[0-9a-f]{6})/gi)].map((m) => [Number(m[1]), m[2]!.toLowerCase()])
    expect(series).toEqual([
      [1, '#2a78d6'], [2, '#eb6834'], [3, '#1baf7a'], [4, '#eda100'], [5, '#4a3aa7'], [6, '#e87ba4'],
    ])
    expect(css).not.toMatch(/--series-[7-9]/)
    for (const file of sources.filter((name) => name.endsWith('.tsx'))) {
      expect(readFileSync(file, 'utf8'), rel(file)).not.toMatch(/#[0-9a-fA-F]{6}\b|rgba?\(/)
    }
  })

  it('浏览器这一侧从不带 SQL Agent 的 Key', () => {
    for (const file of sources) {
      expect(readFileSync(file, 'utf8'), rel(file)).not.toMatch(/['"]X-API-Key['"]\s*:/i)
    }
  })
})
