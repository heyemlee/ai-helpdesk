import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { ChatPanel } from '../ChatPanel'
import { EN, ZH } from '@/i18n/catalog'

describe('conversation interactions', () => {
  beforeEach(() => { Element.prototype.scrollTo = vi.fn() })

  it('lets the user edit a suggested question before sending', async () => {
    const onSend = vi.fn()
    render(<ChatPanel messages={[]} busy={false} onSend={onSend} onInspect={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: '柜门关不严怎么排查？' }))
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveValue('柜门关不严、门缝不齐，应该怎么排查？')
    expect(screen.getByRole('textbox')).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(onSend).toHaveBeenCalledWith('柜门关不严、门缝不齐，应该怎么排查？')
    expect(screen.getByRole('textbox')).toHaveValue('')
  })

  it('selects the order-number placeholder so typing the number replaces it', async () => {
    render(<ChatPanel messages={[]} busy={false} onSend={vi.fn()} onInspect={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: '订单生产到哪一步了？' }))
    const input = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(input).toHaveFocus()
    expect(input.value.slice(input.selectionStart, input.selectionEnd)).toBe('［订单号］')
    await userEvent.keyboard('J25-00003')
    expect(input).toHaveValue('订单J25-00003现在生产到哪个工序了？')
  })

  it('does not send on IME confirmation or while history is being restored', async () => {
    const onSend = vi.fn()
    const { rerender } = render(<ChatPanel messages={[]} busy={false} onSend={onSend} onInspect={vi.fn()} />)
    const input = screen.getByRole('textbox')
    await userEvent.type(input, '测试问题')
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<ChatPanel messages={[]} busy={false} restoring onSend={onSend} onInspect={vi.fn()} />)
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    fireEvent.submit(document.querySelector('form')!)
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not pull a reader away from an older reply as a new chunk arrives', () => {
    const message = { id: 'a', role: 'assistant' as const, content: 'Initial response' }
    const props = { busy: true, onSend: vi.fn(), onInspect: vi.fn() }
    const { container, rerender } = render(<ChatPanel {...props} messages={[message]} />)
    const list = container.querySelector('.thread')!
    Object.defineProperties(list, { scrollHeight: { value: 1000 }, clientHeight: { value: 300 }, scrollTop: { value: 0, writable: true } })
    fireEvent.scroll(list)
    const scroll = vi.mocked(Element.prototype.scrollTo)
    scroll.mockClear()
    rerender(<ChatPanel {...props} messages={[{ ...message, content: 'Initial response with more text' }]} />)
    expect(scroll).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /回到最新消息/ })).toBeInTheDocument()
  })
})

describe('empty conversation', () => {
  beforeEach(() => { Element.prototype.scrollTo = vi.fn() })

  it('keeps the first screen to one line; the full scope guide stays in the top-bar popover', () => {
    render(<ChatPanel messages={[]} busy={false} onSend={vi.fn()} onInspect={vi.fn()}
                      capabilities={{ business_data: true, ticket_notifications: false }} />, { lang: 'en' })
    expect(screen.getByRole('heading', { name: 'What does the customer need?' })).toBeInTheDocument()
    expect(screen.queryByText(/Looked up live from the business systems/)).not.toBeInTheDocument()
    expect(screen.queryByText(/does not mean someone has been notified/)).not.toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'Try a question' })).getAllByRole('button')).toHaveLength(4)
  })

  it('drops the order example only when the server says order lookup is not connected', () => {
    const props = { messages: [], busy: false, onSend: vi.fn(), onInspect: vi.fn() }
    const { rerender } = render(
      <ChatPanel {...props} capabilities={{ business_data: false, ticket_notifications: false }} />, { lang: 'en' })
    expect(screen.queryByRole('button', { name: /order in production/ })).not.toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'Try a question' })).getAllByRole('button')).toHaveLength(3)
    // 后端没报（老版本）不等于没接：照常给
    rerender(<ChatPanel {...props} />)
    expect(screen.getByRole('button', { name: /order in production/ })).toBeInTheDocument()
  })

  it('moves the same input to the bottom after the first message, so focus stays put', async () => {
    const onSend = vi.fn()
    const props = { busy: false, onSend, onInspect: vi.fn() }
    const { container, rerender } = render(<ChatPanel {...props} messages={[]} />)
    const input = screen.getByRole('textbox')
    expect(container.querySelector('.composer-wrap')).toHaveClass('is-empty')
    await userEvent.type(input, '测试问题{Enter}')
    expect(onSend).toHaveBeenCalledWith('测试问题')
    rerender(<ChatPanel {...props} messages={[{ id: 'u1', role: 'user', content: '测试问题' }]} />)
    expect(container.querySelector('.composer-wrap')).not.toHaveClass('is-empty')
    expect(screen.queryByRole('group', { name: '试试这些问题' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox')).toBe(input)
    expect(input).toHaveFocus()
  })

  it('keeps the placeholder text inside the order example in every language', () => {
    for (const catalog of [EN, ZH]) expect(catalog['cw.sample.1']).toContain(catalog['cw.sample.slot'])
  })
})
