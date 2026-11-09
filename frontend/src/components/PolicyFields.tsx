import { useT } from '@/i18n'
import type { PolicyMetadata, PolicyScope } from '@/api/types'

export const draftPolicy: PolicyMetadata = {
  status: 'draft', version: '', product: '*', region: '*', audience: '*', effective_at: null, expires_at: null,
}
export function PolicyFields({ value, onChange }: { value: PolicyMetadata; onChange: (value: PolicyMetadata) => void }) {
  const { lang } = useT()
  const text = (zh: string, en: string) => lang === 'en' ? en : zh
  return <fieldset className="policy-fields"><legend>{text('政策适用条件', 'Policy applicability')}</legend>
    <p>{text('只有已发布且处于生效期间的政策用于回答。* 表示明确通用；历史资料需要审核后发布。', 'Replies use published, effective policies. * explicitly means universal. Review legacy content before publishing.')}</p>
    <label>{text('发布状态', 'Publication status')}<select className="field" value={value.status} onChange={e => onChange({ ...value, status: e.target.value as PolicyMetadata['status'] })}>
      <option value="draft">{text('待审核', 'Draft')}</option><option value="published">{text('已发布', 'Published')}</option><option value="retired">{text('已撤回', 'Retired')}</option>
    </select></label>
    {(['version', 'product', 'region', 'audience'] as const).map((key, i) => <label key={key}>
      {(lang === 'en' ? ['Policy version', 'Product', 'Region', 'Audience'] : ['政策版本', '适用产品', '适用区域', '适用受众'])[i]}
      <input className="field" required={key !== 'version' || value.status === 'published'} value={value[key]} onChange={e => onChange({ ...value, [key]: e.target.value })} />
    </label>)}
    {(['effective_at', 'expires_at'] as const).map((key, i) => <label key={key}>
      {i ? text('失效时间（可空）', 'Expiry time (optional)') : text('生效时间', 'Effective time')}
      <input className="field" placeholder="2026-11-06T00:00:00Z" value={value[key] ?? ''}
        required={!i && value.status === 'published'} onChange={e => onChange({ ...value, [key]: e.target.value || null })} />
    </label>)}
    <p>{text('时间需包含时区，例如 2026-11-06T00:00:00Z。', 'Include a timezone, for example 2026-11-06T00:00:00Z.')}</p>
  </fieldset>
}

export function ScopeFields({ value, onChange }: { value: PolicyScope; onChange: (value: PolicyScope) => void }) {
  const { lang } = useT()
  const text = (zh: string, en: string) => lang === 'en' ? en : zh
  return <fieldset><legend>{text('本次政策范围', 'Policy scope')}</legend>
    <p>{text('未知条件留空，只匹配通用政策。时间留空表示现在；历史时间仅用于员工回溯。', 'Leave unknown dimensions empty to match universal policies. Empty time means now; historical requests require an employee identity.')}</p>
    {(['product', 'region', 'audience', 'as_of'] as const).map((key, i) => <label key={key}>
      {(lang === 'en' ? ['Product', 'Region', 'Audience', 'Historical time (optional, with timezone)'] : ['产品', '区域', '受众', '历史适用时间（可空，需时区）'])[i]}
      <input className="field" value={value[key] ?? ''} onChange={e => onChange({ ...value, [key]: e.target.value || (key === 'as_of' ? null : '') })} />
    </label>)}
  </fieldset>
}
