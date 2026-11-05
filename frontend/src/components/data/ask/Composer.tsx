import { useLayoutEffect, type FormEvent, type KeyboardEvent, type RefObject } from 'react'
import { useT } from '@/i18n'
import { Icon } from '../../Icon'

const MAX_INPUT_HEIGHT = 180

/**
 * 输入框。等结果的时候发送键原地变成"停止"：一个点不了的灰色按钮什么也没告诉人。
 * Enter 发送，Shift+Enter 换行；输入法正在组字时的回车是在选字，不算发送。
 * 输入框空着时按 ↑：把上一个问题取回来，改一改再发。
 */
export function Composer({ value, onChange, onSubmit, onStop, busy, docked, lastQuestion, inputRef, onNew }: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  busy: boolean
  /** 对话里停靠在底部（追问）；首页上是大输入框 */
  docked: boolean
  lastQuestion: string
  inputRef: RefObject<HTMLTextAreaElement>
  onNew: () => void
}) {
  const { t } = useT()

  // 自动长高到上限；没到上限时不留滚动条：自动长高的输入框旁边多一截滚动条只会让人以为还有内容
  useLayoutEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = 'auto'
    input.style.height = `${Math.min(input.scrollHeight, MAX_INPUT_HEIGHT)}px`
    input.style.overflowY = input.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden'
  }, [value, inputRef])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!busy && value.trim()) onSubmit()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    if (event.key === 'ArrowUp' && !value && lastQuestion) {
      event.preventDefault()
      onChange(lastQuestion)
      return
    }
    if (event.key !== 'Enter' || event.shiftKey) return
    event.preventDefault()
    if (!busy && value.trim()) onSubmit()
  }

  return (
    <>
      <form className={`sq-composer${docked ? ' is-dock' : ''}`} onSubmit={submit}>
        <label className="sr-only" htmlFor="sqAskInput">{t('sq.ask.inputLabel')}</label>
        <textarea id="sqAskInput" ref={inputRef} rows={docked ? 1 : 2} autoComplete="off" value={value}
                  placeholder={t(docked ? 'sq.ask.followUpPlaceholder' : 'sq.ask.placeholder')}
                  onChange={(event) => onChange(event.target.value)} onKeyDown={onKeyDown} />
        <div className="sq-composer-bar">
          {docked
            ? (
              <button type="button" className="btn btn-ghost btn-sm" disabled={busy} title={t('sq.ask.newHint')} onClick={onNew}>
                <Icon name="plus" size={14} />{t('sq.ask.new')}
              </button>
            )
            : <span className="sq-composer-meta" title={t('sq.ask.readOnlyHint')}><Icon name="shield" size={14} />{t('sq.ask.readOnly')}</span>}
          <span className="sq-composer-keys" aria-hidden="true">{t('sq.ask.keys')}</span>
          {busy
            ? (
              <button className="sq-send-btn is-stop" type="button" aria-label={t('sq.ask.stop')} title={t('sq.ask.stop')}
                      onClick={onStop}>
                <span className="sq-stop-mark" aria-hidden="true" />
              </button>
            )
            : (
              <button className="sq-send-btn" type="submit" aria-label={t('sq.ask.send')} title={t('sq.ask.send')}
                      disabled={!value.trim()}>
                <Icon name="send" size={16} />
              </button>
            )}
        </div>
      </form>
      {/* "重要的数字先核对"那句提醒跟着答案走：对话里一直在输入框下面，首页上没有答案，不放 */}
      {docked && <p className="sq-composer-caution">{t('sq.ask.caution')}</p>}
    </>
  )
}
