import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import { useT } from '@/i18n'
import { CATALOG } from '@/i18n/catalog'
import { href, parseRoute } from '../route'
import { dateTime } from '../format'
import { explainError } from '../errors'
import { ApiError, setStaffKey } from '@/api/client'

describe('页面位置', () => {
  it('默认是对话；认不出的位置也回到对话', () => {
    expect(parseRoute('')).toEqual({ view: 'chat' })
    expect(parseRoute('#/chat')).toEqual({ view: 'chat' })
    expect(parseRoute('#/nowhere/else')).toEqual({ view: 'chat' })
  })

  it('知识库、工单、管理区各自的位置', () => {
    expect(parseRoute('#/knowledge')).toEqual({ view: 'knowledge', mode: 'browse' })
    expect(parseRoute('#/knowledge/ask')).toEqual({ view: 'knowledge', mode: 'ask' })
    expect(parseRoute('#/knowledge/doc/legacy_6651')).toEqual({ view: 'knowledge', mode: 'browse', docId: 'legacy_6651' })
    expect(parseRoute('#/tickets')).toEqual({ view: 'tickets' })
    expect(parseRoute('#/tickets/tk_90390d64adba')).toEqual({ view: 'tickets', ticketId: 'tk_90390d64adba' })
    expect(parseRoute('#/admin')).toEqual({ view: 'admin', tab: 'overview' })
    expect(parseRoute('#/admin/sops')).toEqual({ view: 'admin', tab: 'sops' })
    expect(parseRoute('#/admin/unknown')).toEqual({ view: 'admin', tab: 'overview' })
  })

  it('链接和解析互为往返，id 里的特殊字符不丢', () => {
    expect(parseRoute(href.document('doc a/b'))).toEqual({ view: 'knowledge', mode: 'browse', docId: 'doc a/b' })
    expect(parseRoute(href.ticket('tk_1'))).toEqual({ view: 'tickets', ticketId: 'tk_1' })
    expect(parseRoute(href.admin('inspector'))).toEqual({ view: 'admin', tab: 'inspector' })
    expect(parseRoute(href.knowledge('ask'))).toEqual({ view: 'knowledge', mode: 'ask' })
  })
})

describe('时间', () => {
  it('拿不到合法时间就不显示，不显示成 1970 年', () => {
    expect(dateTime(0, 'en')).toBe('')
    expect(dateTime(null, 'en')).toBe('')
    expect(dateTime(Number.NaN, 'zh')).toBe('')
    expect(dateTime(1_759_617_315, 'en')).toMatch(/2025/)
  })
})

describe('接口错误说明', () => {
  const t = (key: string) => key
  it('按状态码说下一步，并保留后端原话', () => {
    expect(explainError(new ApiError(401, '401 Unauthorized: 缺少或错误的 X-API-Key'), t as never))
      .toEqual({ body: 'err.401', detail: '401 Unauthorized: 缺少或错误的 X-API-Key' })
    expect(explainError(new ApiError(404, '404'), t as never, 'kb.error.404' as never).body).toBe('kb.error.404')
    expect(explainError(new ApiError(404, '404'), t as never).body).toBe('err.server')
    expect(explainError(new ApiError(503, '503'), t as never).body).toBe('err.503')
    expect(explainError(new TypeError('Failed to fetch'), t as never))
      .toEqual({ body: 'err.network', detail: 'Failed to fetch' })
  })

  it('401：没填钥匙和钥匙不对是两句话——后端回的是同一句，该做的事不一样', () => {
    const rejected = new ApiError(401, '401 Unauthorized: 缺少或错误的 X-API-Key')
    try {
      setStaffKey('')
      expect(explainError(rejected, t as never).body).toBe('err.401')
      setStaffKey('not-the-right-key')
      expect(explainError(rejected, t as never).body).toBe('err.401.rejected')
    } finally {
      setStaffKey('')
    }
  })
})

describe('文案', () => {
  function Count({ n }: { n: number }) {
    const { t } = useT()
    return <span data-testid="count">{t('kb.count.docs', { n })} / {t('kb.count.passages', { n })}</span>
  }

  it('英文按数量选单复数，中文只有一种写法', () => {
    const one = render(<Count n={1} />, { lang: 'en' })
    expect(screen.getByTestId('count')).toHaveTextContent('1 document / 1 passage')
    one.unmount()
    const many = render(<Count n={8} />, { lang: 'en' })
    expect(screen.getByTestId('count')).toHaveTextContent('8 documents / 8 passages')
    many.unmount()
    render(<Count n={1} />, { lang: 'zh' })
    expect(screen.getByTestId('count')).toHaveTextContent('1 篇文档 / 1 个片段')
  })

  it('带单复数的英文键只有两段，中文键没有分隔符', () => {
    for (const [key, text] of Object.entries(CATALOG.en)) {
      const forms = text.split('|')
      expect(forms.length, key).toBeLessThanOrEqual(2)
      if (forms.length === 2) expect(text, key).toContain('{n}')
    }
    for (const [key, text] of Object.entries(CATALOG.zh)) expect(text, key).not.toContain('|')
  })

  it('工单和通知的文案里没有后端给不出依据的承诺', () => {
    // 只查肯定句式。"does not mean someone has been notified" 这类否定说法正是要写的话。
    const claims = /人工已收到|已有人接手|马上回电|稍后会有同事|will call you|call you back|a colleague will|someone has (received|picked)/i
    for (const table of [CATALOG.en, CATALOG.zh]) {
      for (const [key, text] of Object.entries(table)) expect(text, key).not.toMatch(claims)
    }
  })
})
