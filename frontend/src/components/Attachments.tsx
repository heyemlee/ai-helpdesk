import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { ApiError, attachmentApi } from '@/api/client'
import type { Attachment, ImageReview } from '@/api/types'
import { useT } from '@/i18n'
import { Icon } from './Icon'
import './attachments.css'

export function AttachmentView({ base, id, name, compact }: { base: string; id: string; name?: string; compact?: boolean }) {
  const { lang } = useT()
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const en = lang === 'en'
  useEffect(() => {
    let active = true, objectUrl = ''
    setUrl(''); setError('')
    attachmentApi.content(base, id, true).then(blob => {
      if (active) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl) }
    }).catch(() => { if (active) setError(en ? 'Preview unavailable. Check access and retry.' : '预览不可用，请检查登录权限后重试。') })
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [base, id, attempt, en])
  async function download() {
    try {
      const blob = await attachmentApi.content(base, id)
      const source = URL.createObjectURL(blob), link = document.createElement('a')
      link.href = source; link.download = name || id; link.click()
      setTimeout(() => URL.revokeObjectURL(source), 1000)
    } catch { setError(en ? 'Download denied or unavailable.' : '下载不可用或权限已失效。') }
  }
  // The chat composer shows a thumbnail and the file name only; the full card belongs to the thread and the Case page.
  if (compact) return <figure className="attachment-chip">
    {url ? <img src={url} alt={name || (en ? 'Case image' : '工单图片')} width={40} height={40} /> : <span className="attachment-chip-blank" aria-hidden="true" />}
    <figcaption>{name || id}</figcaption>
    {error && <>
      <p role="alert" className="sr-only">{error}</p>
      <button type="button" className="attachment-chip-retry" title={error} aria-label={en ? 'Reload preview' : '重试预览'}
        onClick={() => setAttempt(v => v + 1)}><Icon name="refresh" size={14} /></button>
    </>}
  </figure>
  return <figure className="attachment-card">
    {url && <img src={url} alt={name || (en ? 'Case image' : '工单图片')} />}
    <figcaption>{name || id}</figcaption>
    {error && <p role="alert">{error}</p>}
    <button type="button" className="btn btn-secondary" onClick={download}>{en ? 'Download original' : '下载原图'}</button>
    <button type="button" className="btn btn-secondary" onClick={() => setAttempt(v => v + 1)}>{en ? 'Reload preview' : '重试预览'}</button>
  </figure>
}

/** Upload state shared by the picker below and the chat composer, which lays the same controls out differently. */
export function useAttachmentUpload({ base, value, onChange, onBusy }: {
  base: string; value: Attachment[]; onChange: (files: Attachment[]) => void; onBusy?: (busy: boolean) => void
}) {
  const { lang } = useT(), en = lang === 'en'
  const [error, setError] = useState(''), [uploading, setUploading] = useState(false)
  const retry = useRef<{ file: File; key: string } | null>(null)
  async function upload(file: File, key: string) {
    if (file.size > 8 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      setError(en ? 'Choose PNG, JPEG or WebP, up to 8 MiB.' : '请选择 8 MiB 以内的 PNG、JPEG 或 WebP。'); return
    }
    retry.current = { file, key }; setUploading(true); onBusy?.(true); setError('')
    try {
      const image = await attachmentApi.upload(base, file, key)
      onChange([...value, image]); retry.current = null
    } catch (e) {
      setError(e instanceof ApiError && [401, 403].includes(e.status)
        ? (en ? 'Sign in with your employee account.' : '请使用员工账号登录。')
        : (en ? 'Upload failed. Your file is retained for retry.' : '上传失败，文件已保留，可重试。'))
    } finally { setUploading(false); onBusy?.(false) }
  }
  return {
    error, uploading, full: value.length >= 4,
    hint: en ? 'Up to 4 images per message. Image fields require database verification.' : '每条消息最多 4 张；图片字段需与业务记录核实。',
    pick: (e: ChangeEvent<HTMLInputElement>) => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void upload(file, crypto.randomUUID()) },
    remove: (id: string) => { onChange(value.filter(v => v.id !== id)); void attachmentApi.remove(base, id).catch(() => {}) },
    retryable: !!retry.current && !uploading,
    retryUpload: () => { if (retry.current) void upload(retry.current.file, retry.current.key) },
  }
}

export function AttachmentPicker({ base, value, onChange, disabled, onBusy }: {
  base: string; value: Attachment[]; onChange: (files: Attachment[]) => void; disabled?: boolean; onBusy?: (busy: boolean) => void
}) {
  const { lang } = useT(), en = lang === 'en'
  const files = useAttachmentUpload({ base, value, onChange, onBusy })
  return <div className="attachment-picker">
    <label>{en ? 'Add order or delivery image' : '添加订单、标签或配送图片'}
      <input type="file" accept="image/png,image/jpeg,image/webp" disabled={disabled || files.uploading || files.full} onChange={files.pick} />
    </label>
    <small>{files.hint}</small>
    {files.uploading && <p role="status">{en ? 'Uploading…' : '正在上传…'}</p>}
    {files.error && <p role="alert">{files.error}</p>}
    {files.retryable && <button type="button" onClick={files.retryUpload}>{en ? 'Retry upload' : '重试上传'}</button>}
    <div className="attachment-grid">{value.map(a => <div key={a.id}>
      <AttachmentView base={base} id={a.id} name={a.filename} />
      <button type="button" disabled={disabled || files.uploading} onClick={() => files.remove(a.id)}>{en ? 'Remove from message' : '从消息移除'}</button>
    </div>)}</div>
  </div>
}

const outcomes: Record<string, [string, string]> = {
  matched: ['已与业务记录匹配', 'Matched to business record'], conflict: ['存在冲突', 'Conflicting records'],
  case_conflict: ['与工单事实冲突', 'Conflicts with case facts'], unclear: ['图片模糊', 'Unclear image'],
  missing: ['字段或来源缺失', 'Missing field or source'], unavailable: ['业务查询不可用', 'Query unavailable'],
  failed: ['查询失败', 'Query failed'], unverified: ['证据不完整', 'Incomplete evidence'],
  no_record: ['无业务记录', 'No business record'], missing_order: ['缺少明确订单', 'Order reference required'],
  extraction_failed: ['图片读取失败', 'Image extraction failed'], attachment_changed: ['附件校验失败', 'Attachment integrity failed'],
  checked: ['核对完成', 'Review complete'],
}
export const imageFieldLabel = (key: string, en: boolean) => {
  const fields: Record<string, [string, string]> = {
    order_ref: ['订单号', 'Order reference'], model: ['型号', 'Model'], quantity: ['数量', 'Quantity'],
    expected_model: ['应收型号', 'Expected model'], received_model: ['实收型号', 'Received model'],
    status: ['业务状态', 'Business status'], date: ['日期', 'Date'], readability: ['图片核验未完成', 'Image verification incomplete'], review: ['完成图片核对', 'Complete image review'],
  }
  return (key.startsWith('image:') ? (en ? 'Image review: ' : '图片待核实：') : '') +
    (fields[key.replace(/^image:/, '')]?.[en ? 1 : 0] || key)
}
export function ImageReviewTable({ review }: { review: ImageReview }) {
  const { lang } = useT(), en = lang === 'en'
  const outcome = (key: string) => outcomes[key]?.[en ? 1 : 0] || key
  return <div className="image-review"><p>{outcome(review.query_outcome)}</p>
    <table><thead><tr>{(en ? ['Field', 'Image candidate', 'Database value', 'Result', 'Source / time'] : ['字段', '图片候选', '业务记录', '结果', '来源 / 时点']).map(h => <th key={h}>{h}</th>)}</tr></thead>
      <tbody>{review.fields.map((f, i) => <tr key={i}><td>{imageFieldLabel(f.field, en)}</td><td>{f.value || '—'} {f.unit}</td><td>{f.db_value || '—'}</td>
        <td>{outcome(f.outcome)}</td><td>{f.source ? <details><summary>{new Date(f.source.as_of).toLocaleString()}</summary>
          {f.source.database} · {f.source.column} · {f.source.trace_id || f.source.run_id}</details> : '—'}</td></tr>)}</tbody>
    </table>
    <p>{en ? 'Unclear or conflicting fields need clearer material or staff handling. No liability or compensation decision is made.' : '模糊或冲突字段需补充清晰材料或由员工处理；核对不作判责或赔偿决定。'}</p>
  </div>
}
