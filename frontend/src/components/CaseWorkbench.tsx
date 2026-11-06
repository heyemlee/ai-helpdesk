import { ScopeFields } from './PolicyFields'
import type { PolicyScope } from '@/api/types'
import { useCallback, useEffect, useRef, useState } from 'react'
import { SafeMarkdown } from './SafeMarkdown'
import { EvidenceDetails } from './EvidenceDetails'
import { ApiError, caseApi, loadSettings } from '@/api/client'
import type { Attachment, CaseAction, CaseDetail, CaseInput, CaseSummary } from '@/api/types'
import { useT } from '@/i18n'
import { AttachmentPicker, AttachmentView, ImageReviewTable, imageFieldLabel } from './Attachments'

const labels: Record<string, [string, string]> = {
  open: ['待处理', 'Open'], waiting_for_info: ['等待补料', 'Waiting for information'],
  in_progress: ['处理中', 'In progress'], resolved: ['已解决', 'Resolved'], cancelled: ['已取消', 'Cancelled'],
  general: ['一般问题', 'General'], order: ['订单问题', 'Order'], missing_parts: ['缺件', 'Missing parts'], wrong_item: ['错货', 'Wrong item'],
  order_ref: ['订单号', 'Order reference'], model: ['缺件型号', 'Missing model'], quantity: ['缺件数量', 'Missing quantity'],
  expected_model: ['应收型号', 'Expected model'], received_model: ['实收型号', 'Received model'],
}
const fields: Record<string, string[]> = { general: [], order: ['order_ref'], missing_parts: ['order_ref', 'model', 'quantity'], wrong_item: ['order_ref', 'expected_model', 'received_model'] }
const id = () => crypto.randomUUID().replaceAll('-', '')

export function CaseWorkbench({ base, ticketId }: { base: string; ticketId?: string }) {
  const { lang } = useT()
  const en = lang === 'en'
  const text = (zh: string, english: string) => en ? english : zh
  const label = (key: string) => labels[key]?.[en ? 1 : 0] ?? imageFieldLabel(key, en)
  const [list, setList] = useState<CaseSummary[]>([])
  const [selected, setSelected] = useState(ticketId ?? '')
  const [current, setCurrent] = useState<CaseDetail | null>(null)
  const [issue, setIssue] = useState<CaseSummary['issue_type']>('general')
  const [conv, setConv] = useState(() => loadSettings().conversationId || id())
  const [customer, setCustomer] = useState('')
  const [message, setMessage] = useState('')
  const [policyScope, setPolicyScope] = useState<PolicyScope>({})
  useEffect(() => { setPolicyScope(current?.policy_scope ?? {}) }, [current?.id])
  const [facts, setFacts] = useState<Record<string, string>>({})
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [uploading, setUploading] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [available, setAvailable] = useState(false)
  const [notice, setNotice] = useState('')
  const pending = useRef<{ fingerprint: string; body: CaseInput } | null>(null)
  const generation = useRef(0)
  const refreshList = useCallback(async () => {
    const data = await caseApi.list(base)
    setList(data.tickets)
    setAvailable(true)
  }, [base])
  useEffect(() => {
    let active = true
    refreshList().catch((e: unknown) => {
      if (!active) return
      setAvailable(false)
      setError(e instanceof ApiError && e.status === 401
        ? (en ? 'Sign in with your company employee account, then reopen the case workspace.' : '请使用公司员工账号登录，再打开工单工作台。')
        : (en ? 'The case workspace is unavailable. Ask an administrator to check the service, then try again.' : '工单工作台暂不可用，请联系管理员检查服务后重试。'))
    })
    return () => { active = false }
  }, [refreshList, en])
  useEffect(() => {
    const token = ++generation.current
    setCurrent(null)
    setFacts({})
    setError('')
    setNotice('')
    pending.current = null
    if (!selected) return
    caseApi.get(base, selected).then(c => {
      if (generation.current === token) { setCurrent(c); setConv(c.conv_id) }
    }).catch(() => {
      if (generation.current === token) setError(en ? 'This case cannot be read with your current access.' : '当前身份无法读取此工单。')
    })
  }, [base, selected, en])
  const running = current?.runs.some(r => r.status === 'queued' || r.status === 'running')
  useEffect(() => {
    if (!current || !running) return
    let active = true
    const timer = setTimeout(() => {
      caseApi.get(base, current.id).then(c => { if (active) setCurrent(c) }).catch(() => {
        if (active) setError(en ? 'Could not refresh progress. Your input is saved.' : '进度刷新失败，已提交的材料仍然保存。')
      })
    }, 1200)
    return () => { active = false; clearTimeout(timer) }
  }, [base, current, running, en])
  async function perform(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await work() } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        pending.current = null
        setError(text('工单已变化，输入已保留。请核对刷新后的材料再提交。', 'The case changed. Your input is retained. Review the refreshed case before submitting.'))
        if (selected) { try { setCurrent(await caseApi.get(base, selected)) } catch { /* retain input */ } }
      } else {
        setError(text('操作未完成，输入已保留。可用同一份输入重试。', 'The action did not complete. Your input is retained; retry the same input.'))
      }
    } finally { setBusy(false) }
  }
  async function submit(retryIds?: string[]) {
    await perform(async () => {
      const cleanFacts = Object.fromEntries(Object.entries(facts).filter(([, v]) => v.trim()))
      const ids = retryIds ?? attachments.map(a => a.id)
      const fingerprint = JSON.stringify({ selected, conv, message, cleanFacts, issue, customer, ids, policyScope })
      if (pending.current?.fingerprint !== fingerprint) {
        pending.current = { fingerprint, body: {
          message_id: id(), message, policy_scope: policyScope, conv_id: current?.conv_id ?? conv, facts: cleanFacts,
          ...(ids.length ? { attachment_ids: ids } : {}),
          ...(current ? { case_id: current.id, expected_version: current.version, expected_control_version: current.control_version }
            : { new_case: true, issue_type: issue, customer_ref: customer }),
        } }
      }
      const receipt = await caseApi.input(base, pending.current.body)
      pending.current = null
      setMessage(''); setFacts({}); setAttachments([])
      setSelected(receipt.case_id)
      setCurrent(await caseApi.get(base, receipt.case_id))
      await refreshList()
      setNotice(text('材料已保存，处理完成后会自动更新。你可以继续补充材料。', 'Material saved. This page will update when processing finishes. You can add more information.'))
    })
  }
  async function act(operation: CaseAction['operation'], status?: CaseAction['status']) {
    if (!current) return
    await perform(async () => {
      setCurrent(await caseApi.act(base, current.id, { operation, status, reason,
        expected_version: current.version, expected_control_version: current.control_version }))
      setReason('')
      await refreshList()
    })
  }
  const closed = current?.status === 'resolved' || current?.status === 'cancelled'
  const required = [...new Set([...(fields[current?.issue_type ?? issue] ?? []), ...(current?.pending_fields ?? [])])].filter(f => !f.startsWith('image:'))
  return <section className="case-workbench" aria-label={text('跨消息工单', 'Case workspace')}>
    <h2>{text('同一问题，持续处理', 'Continue the same business problem')}</h2>
    <p>{text('明确选择已有问题或新建。相同订单可以有多个售后问题，系统不会自动合并。', 'Choose a case or create a new problem. The same order can have several separate issues.')}</p>
    {error && <p role="alert" className="note danger">{error}</p>}
    {notice && <p role="status" className="note neutral">{notice}</p>}
    <ScopeFields value={policyScope} onChange={setPolicyScope} />
    {available && <>
      <label>{text('选择问题', 'Choose case')}
        <select value={selected} disabled={busy || uploading} onChange={e => { setSelected(e.target.value); setMessage(''); setAttachments([]) }}>
          <option value="">{text('新建问题', 'New problem')}</option>
          {list.map(c => <option key={c.id} value={c.id}>{c.id} · {label(c.issue_type)} · {label(c.status)}</option>)}
        </select>
      </label>
      {selected && !current ? <p>{text('正在读取工单…', 'Loading case…')}</p> : <>
        {current ? <>
          <p><strong>{label(current.status)}</strong> · {text('材料版本', 'Material version')} {current.version} · {text('控制权版本', 'Control version')} {current.control_version}</p>
          <p>{text('处理方式', 'Control')}: {current.control_mode === 'human' ? text('人工', 'Human') : text('助手', 'Assistant')}
            {current.owner_actor_id && ` · ${text('负责人', 'Owner')}: ${current.owner_actor_id}`}</p>
          <p>{text('业务客户', 'Customer reference')}: {current.customer_ref || '—'} · {text('订单', 'Orders')}: {current.order_refs.join(', ') || current.facts.order_ref?.value || '—'}</p>
          <div className="case-actions">
            <button className="btn btn-secondary" disabled={busy} onClick={() => act(current.control_mode === 'human' ? 'release' : 'takeover')}>
              {current.control_mode === 'human' ? text('交回助手', 'Release to assistant') : text('人工接管', 'Take over')}
            </button>
            {!closed && <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => act('status', 'resolved')}>{text('标记解决', 'Resolve')}</button>
              <button className="btn btn-secondary" disabled={busy} onClick={() => act('status', 'cancelled')}>{text('取消问题', 'Cancel case')}</button>
            </>}
          </div>
          {closed && <div className="case-reopen"><label>{text('重开原因', 'Reason to reopen')}<input value={reason} onChange={e => setReason(e.target.value)} /></label>
            <button className="btn btn-primary" disabled={busy || !reason.trim()} onClick={() => act('reopen')}>{text('重开问题', 'Reopen')}</button></div>}
          <h3>{text('材料与待补项', 'Materials and missing information')}</h3>
          <dl>{Object.entries(current.facts).map(([field, fact]) => <div key={field}>
            <dt>{label(field)}</dt><dd>{fact.value} · {['verified_evidence', 'verified_image'].includes(fact.source) ? text('证据已核实', 'Verified evidence') : text('员工提交', 'Employee supplied')}</dd>
          </div>)}</dl>
          <p>{text('待补', 'Missing')}: {current.pending_fields.map(label).join(', ') || text('无', 'None')}</p>
          {current.materials.map(m => <div key={m.attachment_id}>
            <AttachmentView base={base} id={m.attachment_id} name={m.attachment?.filename} />
            <p>{text('材料指纹', 'Material hash')}: {m.sha256.slice(0, 12)}</p>
            {current.image_reviews?.filter(r => r.attachment_id === m.attachment_id).slice(-1).map(r =>
              <ImageReviewTable key={r.run_id} review={r.result} />)}
            {!closed && <button type="button" className="btn btn-secondary" disabled={busy || running || uploading}
              onClick={() => submit([m.attachment_id])}>{text('重新核对此图', 'Recheck image')}</button>}
          </div>)}
        </> : <div className="case-fields">
          <label>{text('会话', 'Conversation')}<input value={conv} onChange={e => setConv(e.target.value)} /></label>
          <label>{text('问题类型', 'Issue type')}<select value={issue} onChange={e => { setIssue(e.target.value as typeof issue); setFacts({}) }}>
            {Object.keys(fields).map(k => <option key={k} value={k}>{label(k)}</option>)}
          </select></label>
          <label>{text('业务客户引用', 'Customer reference')}<input value={customer} onChange={e => setCustomer(e.target.value)} /></label>
        </div>}
        {!closed && <form onSubmit={e => { e.preventDefault(); void submit() }}>
          <div className="case-fields">{required.map(field => <label key={field}>{label(field)}
            <input value={facts[field] ?? ''} placeholder={current?.facts[field]?.value ?? ''}
              onChange={e => setFacts({ ...facts, [field]: e.target.value })} />
          </label>)}</div>
          <label>{text('补充说明', 'Message')}<textarea value={message} onChange={e => setMessage(e.target.value)} maxLength={4000} /></label>
          <AttachmentPicker base={base} value={attachments} onChange={setAttachments} onBusy={setUploading} disabled={busy} />
          <button className="btn btn-primary" disabled={busy || uploading || !conv.trim() || (!message.trim() && !attachments.length && !Object.values(facts).some(v => v.trim()))}>
            {busy ? text('正在保存…', 'Saving…') : text('保存并继续', 'Save and continue')}
          </button>
        </form>}
        {current && <>
          {running && <p role="status">{text('材料已保存，正在处理。', 'Material saved. Processing.')}</p>}
          <h3>{text('答复草稿', 'Reply drafts')}</h3>
          {current.drafts.map(d => <article className="case-draft" key={d.id}>
            <p>{text('版本', 'Version')} {d.version} · {d.valid ? (d.status === 'approved' ? text('已确认', 'Approved') : text('待确认', 'Pending approval')) : text('已失效', 'Invalidated')}</p>
            <SafeMarkdown>{d.body}</SafeMarkdown>
            <EvidenceDetails records={d.evidence} />
            <button className="btn btn-secondary" disabled={busy || !d.valid || d.status === 'approved'} onClick={() => perform(async () => { setCurrent(await caseApi.approve(base, current, d)) })}>
              {text('确认此版本草稿', 'Approve this version')}
            </button>
            <p className="note neutral">{text('确认草稿只记录内部审批，不会向客户发送。', 'Approval records an internal decision; it does not send a message to the customer.')}</p>
          </article>)}
          <h3>{text('本问题历史', 'Case history')}</h3>
          {current.messages.map(m => <p key={m.message_id}><strong>{m.role === 'user' ? text('员工', 'Employee') : text('助手', 'Assistant')} · v{m.version}</strong> {m.content}</p>)}
          <details><summary>{text('处理记录', 'Execution history')}</summary>
            {current.runs.map(r => <p key={r.run_id}>{r.run_id} · {r.status ?? text('执行记录已过保留期', 'Execution record expired')}{r.applied === false ? ` · ${text('未采纳', 'Not applied')}: ${r.reason}` : ''}</p>)}
            {current.events.map(e => <p key={e.id}>{e.kind} · v{e.version}{e.actor_id ? ` · ${e.actor_id}` : ''}{e.data.reason ? ` · ${String(e.data.reason)}` : ''}</p>)}
          </details>
        </>}
      </>}
    </>}
  </section>
}
