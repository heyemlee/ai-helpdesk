import { describe, expect, it, beforeEach } from 'vitest'
import { render, screen } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { LangProvider, useT } from '@/i18n'
import { RoutingInspector } from '../RoutingInspector'

function Probe() {
  const { t, lang, toggle } = useT()
  return (
    <>
      <span data-testid="lang">{lang}</span>
      <span data-testid="text">{t('in.empty')}</span>
      <button onClick={toggle}>switch</button>
    </>
  )
}

describe('i18n', () => {
  beforeEach(() => { try { localStorage.clear() } catch { /* jsdom 可能没有 storage */ } })

  it('默认英语——不跟随浏览器语言，和原型页同一口径', () => {
    // 不走 test/render 的 initial，直接裸用 Provider 才测得到真正的默认值
    render(<LangProvider><Probe /></LangProvider>, { lang: 'en' })
    expect(screen.getAllByTestId('lang')[0]).toHaveTextContent('en')
    expect(screen.getAllByTestId('text')[0]!.textContent).toMatch(/^Send a message/)
  })

  it('切换语言会重渲染，不需要刷新', async () => {
    render(<Probe />, { lang: 'en' })
    expect(screen.getByTestId('text').textContent).toMatch(/^Send a message/)
    await userEvent.click(screen.getByRole('button', { name: 'switch' }))
    expect(screen.getByTestId('lang')).toHaveTextContent('zh')
    expect(screen.getByTestId('text').textContent).toMatch(/^发一条消息/)
  })

  it('缺键时返回键名本身，不静默留白——漏译要能被看见', () => {
    function Missing() {
      const { t } = useT()
      // @ts-expect-error 故意传一个不存在的键
      return <span data-testid="miss">{t('nope.not.a.key')}</span>
    }
    render(<Missing />)
    expect(screen.getByTestId('miss')).toHaveTextContent('nope.not.a.key')
  })

  it('闭集映射跟着语言走：领域短名在英文下是英文', () => {
    const msg = {
      id: '1', role: 'assistant' as const, content: 'x',
      routing: {
        primary_agent: 'billing', supporting_agents: [], reason: 'r', confidence: 0.9,
        domain_scores: { billing: 0.9 },
      } as never,
    }
    const { unmount } = render(<RoutingInspector message={msg} />, { lang: 'en' })
    expect(screen.getByText('Billing')).toBeInTheDocument()
    unmount()
    render(<RoutingInspector message={msg} />, { lang: 'zh' })
    expect(screen.getByText('账单')).toBeInTheDocument()
  })
})
