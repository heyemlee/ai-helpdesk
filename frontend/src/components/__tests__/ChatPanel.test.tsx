import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@/test/render'
import userEvent from '@testing-library/user-event'
import { ChatPanel } from '../ChatPanel'

describe('conversation interactions', () => {
  beforeEach(() => { Element.prototype.scrollTo = vi.fn() })

  it('lets the user edit a suggested question before sending', async () => {
    const onSend = vi.fn()
    render(<ChatPanel messages={[]} busy={false} onSend={onSend} onInspect={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /产品报价/ }))
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveValue('石英石台面多少钱一米？')
    expect(screen.getByRole('textbox')).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(onSend).toHaveBeenCalledWith('石英石台面多少钱一米？')
    expect(screen.getByRole('textbox')).toHaveValue('')
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
