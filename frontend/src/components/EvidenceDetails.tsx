import { useT } from '@/i18n'

type RecordValue = Record<string, unknown>
const record = (v: unknown): RecordValue => v && typeof v === 'object' && !Array.isArray(v) ? v as RecordValue : {}
const show = (v: unknown) => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)
const labels: Record<string, [string, string]> = {
  order_ref: ['订单', 'Order'], model: ['型号', 'Model'], sku: ['型号', 'SKU'], amount: ['金额', 'Amount'],
  total_amount: ['总金额', 'Total amount'], quantity: ['数量', 'Quantity'], missing_qty: ['缺件数量', 'Missing quantity'],
  status: ['状态', 'Status'], ship_date: ['发货日期', 'Ship date'], delivery_date: ['配送日期', 'Delivery date'],
}
const outcomes: Record<string, [string, string]> = {
  no_data: ['查询成功，但没有匹配记录', 'Query succeeded with no matching records'],
  failed: ['查询失败', 'Query failed'], partial_success: ['只有部分查询成功', 'Only part of the query succeeded'],
  truncated: ['结果被截断', 'Result was truncated'], missing_unit: ['缺少确认的单位', 'Confirmed units are missing'],
  missing_or_stale_time: ['查询时间缺失或已过期', 'Observation time is missing or expired'],
  missing_provenance: ['缺少可追溯来源', 'Traceable source is missing'], conflicting_records: ['业务记录存在冲突', 'Business records conflict'],
  missing_evidence: ['缺少结构化证据', 'Structured evidence is missing'], invalid_evidence: ['证据与查询记录不一致', 'Evidence does not match the query records'],
}

/** Same row grouping and short references as the server's deterministic reply. */
export function EvidenceDetails({ records }: { records: unknown[] }) {
  const { lang } = useT()
  const en = lang === 'en'
  const text = (zh: string, english: string) => en ? english : zh
  const items = records.map(record)
  const business = items.filter(r => Array.isArray(r.evidence))
  const cells = business.flatMap(r => (r.evidence as unknown[]).map(record))
  const groups = new Map<string, RecordValue[]>()
  for (const cell of cells) {
    const key = JSON.stringify([cell.database, cell.step_index, cell.row_index, cell.query_hash])
    groups.set(key, [...(groups.get(key) ?? []), cell])
  }
  const policies = items.filter(r => r.policy_revision)
  const images = items.filter(r => r.source === 'verified_image' && r.reply_reference)
  return <details className="evidence-details">
    <summary>{text('可核对的依据', 'Evidence to review')} ({groups.size + policies.length + images.length})</summary>
    {[...groups.values()].map((group, i) => <section key={i}>
      <h4>[{i + 1}] {Object.entries(record(group[0]!.entity_keys)).map(([k, v]) => `${labels[k]?.[en ? 1 : 0] ?? k}: ${show(v)}`).join(' · ') || text('查询记录', 'Query record')}</h4>
      <p>{text('数据时点', 'Data as of')}: {show(group[0]!.as_of)} · {text('来源', 'Source')}: {show(group[0]!.database)}</p>
      <table><thead><tr>{[text('字段', 'Field'), text('值', 'Value'), text('单位', 'Unit')].map(h => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{group.map((c, j) => <tr key={j}><td>{labels[String(c.field)]?.[en ? 1 : 0] ?? show(c.field)}</td><td>{show(c.value)}</td><td>{show(c.unit)}</td></tr>)}</tbody></table>
      <details><summary>{text('完整追踪信息', 'Full trace details')}</summary><pre>{JSON.stringify(group, null, 2)}</pre></details>
    </section>)}
    {policies.map((p, i) => <section key={`p${i}`}><h4>[P{i + 1}] {show(p.doc_title ?? p.title)}</h4>
      <dl>{(['version', 'product', 'region', 'audience', 'effective_at', 'expires_at'] as const).map((key, j) => <div key={key}>
        <dt>{(en ? ['Version', 'Product', 'Region', 'Audience', 'Effective from', 'Expires at'] : ['版本', '产品', '区域', '受众', '生效时间', '失效时间'])[j]}</dt>
        <dd>{record(p.policy)[key] === '*' ? text('通用', 'Universal') : show(record(p.policy)[key])}</dd>
      </div>)}</dl><p>{text('适用时点', 'Evaluated as of')}: {show(p.policy_as_of)}</p>
      <p>{show(p.content ?? p.excerpt)}</p>
      <details><summary>{text('完整追踪信息', 'Full trace details')}</summary><pre>{JSON.stringify(p, null, 2)}</pre></details>
    </section>)}
    {images.map((p, i) => <section key={`i${i}`}><h4>[{show(p.reply_reference)}] {labels[String(p.field)]?.[en ? 1 : 0] ?? show(p.field)}</h4>
      <p>{show(p.db_value)} {show(p.unit)} · {show(record(p.business_source).as_of)}</p>
      <details><summary>{text('图片与业务记录', 'Image and business record')}</summary><pre>{JSON.stringify(p, null, 2)}</pre></details>
    </section>)}
    {business.map((r, i) => Array.isArray(r.evidence_issues) && r.evidence_issues.length > 0 && <section key={`issue${i}`}>
      <p>{text('尚待核实', 'Needs verification')}: {r.evidence_issues.map(issue => outcomes[String(issue)]?.[en ? 1 : 0] ?? text('依据尚不完整', 'Evidence is incomplete')).join('; ')}</p>
      <details><summary>{text('完整追踪信息', 'Full trace details')}</summary><pre>{JSON.stringify(r, null, 2)}</pre></details>
    </section>)}
  </details>
}
