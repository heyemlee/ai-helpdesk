import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { authHeaders, loadSettings, setCsrfToken, setSessionIdentity } from '@/api/client'

type Employee = { actor_id: string; instance: string; roles: string[]; auth_version: number }
type Session = { actor: Employee; csrf_token: string }
export const EmployeeContext = createContext<Employee | null>(null)
export const useEmployee = () => useContext(EmployeeContext)
export function usePermissions() {
  const actor = useEmployee()
  const admin = !!actor?.roles.includes('admin')
  return { admin, operations: admin || !!actor?.roles.includes('operations') }
}

export function clearEmployeeStorage() {
  for (const storage of [localStorage, sessionStorage]) {
    for (const key of Object.keys(storage)) {
      if (/^(ai-helpdesk|helpdesk|sql)/.test(key) && !['helpdesk.lang', 'helpdesk.theme'].includes(key)) storage.removeItem(key)
    }
  }
}

export function SessionGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [checking, setChecking] = useState(true)
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const queries = useQueryClient()
  const checks = useRef(0)
  const changingIdentity = useRef(false)
  const identity = useRef(sessionStorage.getItem('ai-helpdesk.session-owner') || '')
  // Employee credentials are sent only to the same-origin gateway.
  const base = loadSettings().apiBaseUrl
  function forget() {
    setCsrfToken(''); setSessionIdentity(''); identity.current = ''; setSession(null); queries.clear(); clearEmployeeStorage()
  }
  useEffect(() => {
    let active = true
    const check = async () => {
      if (changingIdentity.current) return
      const generation = ++checks.current
      const current = () => active && generation === checks.current
      try {
        const response = await fetch(base + '/auth/session', { credentials: 'same-origin', cache: 'no-store' })
        if (response.status === 401) { if (current()) forget(); return }
        if (!response.ok) throw new Error('unavailable')
        const value: Session = await response.json()
        if (current()) {
          const next = value.actor.actor_id + ':' + value.actor.auth_version
          if (identity.current !== next) { clearEmployeeStorage(); queries.clear() }
          identity.current = next; sessionStorage.setItem('ai-helpdesk.session-owner', next); setSessionIdentity(next)
          setSession(value); setCsrfToken(value.csrf_token); setError('')
        }
      } catch { if (current()) setError('暂时无法验证会话，已保留当前输入和恢复记录，连接恢复后会重试。') }
      finally { if (current()) setChecking(false) }
    }
    void check()
    const timer = setInterval(() => void check(), 30000)
    const expired = () => { ++checks.current; if (active) forget() }
    window.addEventListener('employee-session-expired', expired)
    window.addEventListener('focus', check)
    return () => { active = false; ++checks.current; clearInterval(timer); window.removeEventListener('employee-session-expired', expired); window.removeEventListener('focus', check) }
    // The gateway origin is fixed for this page lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base])
  if (checking) return <main className="login-page"><p>正在验证员工会话…</p></main>
  if (!session) return <main className="login-page"><form className="login-card" onSubmit={async event => {
    event.preventDefault(); changingIdentity.current = true; ++checks.current; setBusy(true); setError('')
    try {
      const response = await fetch(base + '/auth/login', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login, password }) })
      if (!response.ok) throw new Error(response.status === 429 ? '登录尝试过多，请稍后重试。' : '账号或密码无效，或登录服务暂不可用。')
      const value: Session = await response.json()
      clearEmployeeStorage(); identity.current = value.actor.actor_id + ':' + value.actor.auth_version; sessionStorage.setItem('ai-helpdesk.session-owner', identity.current); setSessionIdentity(identity.current); queries.clear(); setPassword(''); setCsrfToken(value.csrf_token); setSession(value)
    } catch (e) { setError(e instanceof Error ? e.message : '登录失败') }
    finally { changingIdentity.current = false; setBusy(false) }
  }}>
    <p className="eyebrow">AI HELPDESK</p><h1>员工登录</h1>
    <p>使用管理员分配的员工账号访问工作台。</p>
    <label>员工账号<input className="field" autoComplete="username" value={login} onChange={e => setLogin(e.target.value)} required /></label>
    <label>密码<input className="field" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
    {error && <p role="alert">{error}</p>}
    <button className="btn btn-primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
  </form></main>
  return <EmployeeContext.Provider value={session.actor} key={session.actor.actor_id + ':' + session.actor.auth_version}>
    <div className="employee-session"><span>{session.actor.actor_id} · {session.actor.roles.join(', ')}</span>
      <button className="btn btn-ghost" onClick={async () => {
        changingIdentity.current = true; ++checks.current; setBusy(true)
        try {
          const response = await fetch(base + '/auth/logout', { method: 'POST', credentials: 'same-origin', headers: authHeaders() })
          if (!response.ok && response.status !== 401) throw new Error()
          forget()
        } catch { setError('退出未完成，请重试。') }
        finally { changingIdentity.current = false; setBusy(false) }
      }} disabled={busy}>退出登录</button>{error && <span role="alert">{error}</span>}
    </div>{children}
  </EmployeeContext.Provider>
}
