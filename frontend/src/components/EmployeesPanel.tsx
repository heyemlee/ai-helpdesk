import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { authHeaders } from '@/api/client'
import { Card } from './ui'

type Account = { actor_id: string; login: string; role: string; active: boolean }
export function EmployeesPanel({ base }: { base: string }) {
  const [login, setLogin] = useState(''), [password, setPassword] = useState(''), [role, setRole] = useState('employee')
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const accounts = useQuery({ queryKey: ['employees', base], queryFn: async () => {
    const r = await fetch(base + '/admin/employees', { credentials: 'same-origin' })
    if (!r.ok) throw new Error('无法读取员工账号')
    return (await r.json()).employees as Account[]
  } })
  async function change(path: string, method: string, body: unknown) {
    setBusy(true); setError('')
    try {
      const r = await fetch(base + '/admin/employees' + path, { method, credentials: 'same-origin',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!r.ok) throw new Error('保存失败，请检查账号、密码长度与管理员权限。最后一个管理员不能停用。')
      setPassword(''); await accounts.refetch()
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败') }
    finally { setBusy(false) }
  }
  return <Card title="员工账号">
    <p>账号停用、角色变更或密码重置会立即撤销全部会话和待执行任务。</p>
    {(error || accounts.isError) && <p role="alert">{error || '无法读取员工账号'}</p>}
    <table><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>
      {accounts.data?.map(a => <tr key={a.actor_id}><td>{a.login}</td><td><select aria-label={a.login + ' 角色'} value={a.role} disabled={busy}
        onChange={e => void change('/' + a.actor_id, 'PATCH', { role: e.target.value })}>
        <option value="employee">员工</option><option value="operations">运营</option><option value="admin">管理员</option>
      </select></td><td>{a.active ? '启用' : '停用'}</td><td><button className="btn btn-secondary" disabled={busy}
        onClick={() => void change('/' + a.actor_id, 'PATCH', { active: !a.active })}>{a.active ? '停用' : '启用'}</button></td></tr>)}
    </tbody></table>
    <form onSubmit={e => { e.preventDefault(); void change('', 'POST', { login, password, role }) }}>
      <label>新员工账号<input className="field" value={login} onChange={e => setLogin(e.target.value)} minLength={3} required /></label>
      <label>初始密码<input className="field" type="password" autoComplete="new-password" value={password} minLength={14} maxLength={128} required onChange={e => setPassword(e.target.value)} /></label>
      <label>角色<select value={role} onChange={e => setRole(e.target.value)}><option value="employee">员工</option><option value="operations">运营</option><option value="admin">管理员</option></select></label>
      <button className="btn btn-primary" disabled={busy}>创建员工账号</button>
    </form>
  </Card>
}
