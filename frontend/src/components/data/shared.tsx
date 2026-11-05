/**
 * 数据页面（问数、数据浏览、查询历史、一次查询的经过）共用的小件。
 */
import { useEffect, useRef, useState } from 'react'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type { HistoryStatus } from '@/lib/sql/model'
import { explainSqlError } from '@/lib/sql/errors'
import { Chip, StateBlock, type Tone } from '../ui'
import { Icon } from '../Icon'

/** 复制一段文字。剪贴板不可用（http 页面、被拒绝）时说复制失败，不假装成功 */
export function CopyButton({ text, label }: { text: string; label?: string }) {
  const { t } = useT()
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const copy = async () => {
    let ok = false
    try {
      await navigator.clipboard.writeText(text)
      ok = true
    } catch { ok = false }
    setState(ok ? 'copied' : 'failed')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }
  return (
    <button type="button" className="btn btn-ghost btn-sm" onClick={() => void copy()}>
      <Icon name={state === 'copied' ? 'check' : 'copy'} size={14} />
      <span aria-live="polite">
        {state === 'copied' ? t('sq.action.copied') : state === 'failed' ? t('sq.action.copyFailed') : label ?? t('sq.action.copySql')}
      </span>
    </button>
  )
}

/** 一段 SQL：等宽、保留换行、可以复制。SQL 是服务端给的原文，按文本渲染（React 默认转义） */
export function SqlCode({ sql, title }: { sql: string; title?: string }) {
  return (
    <div className="sq-code">
      <div className="sq-code-head">
        {title ? <span>{title}</span> : <span />}
        <CopyButton text={sql} />
      </div>
      <pre className="mono">{sql}</pre>
    </div>
  )
}

const HISTORY_TONES: Record<HistoryStatus, Tone> = {
  success: 'success',
  completed: 'neutral',
  no_data: 'info',
  review: 'caution',
  partial: 'caution',
  degraded: 'caution',
  failed: 'danger',
  conversation: 'neutral',
}

/** 一条提问的状态。历史列表、详情和问数页的"最近问过"用同一个，免得同一条记录在两处长得不一样 */
export function HistoryStatusChip({ status }: { status: HistoryStatus }) {
  const { t } = useT()
  return <Chip tone={HISTORY_TONES[status]} dot>{t(`sq.hstatus.${status}` as Key)}</Chip>
}

/** 接口出错：说发生了什么、接下来做什么，服务端原话放在小字里 */
export function SqlErrorBlock({ error, title, notFound, onRetry }: {
  error: unknown
  title: string
  notFound?: Key
  onRetry?: () => void
}) {
  const { t } = useT()
  const { body, detail } = explainSqlError(error, t, notFound)
  return (
    <StateBlock tone="danger" title={title} detail={detail || undefined}
      action={onRetry && (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          <Icon name="refresh" size={14} />{t('sq.action.retry')}
        </button>
      )}>
      {body}
    </StateBlock>
  )
}

/** 带"服务端原话"标注的一段中文诊断：原样显示，标明语言，不翻 */
export function ServerText({ text }: { text: string }) {
  const { t } = useT()
  if (!text) return null
  return (
    <p className="sq-server-text">
      <span className="sq-server-label">{t('sq.err.original')}</span>{' '}
      <span lang={/[㐀-鿿]/.test(text) ? 'zh' : undefined}>{text}</span>
    </p>
  )
}
