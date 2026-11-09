import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionGate } from '../session'

beforeEach(() => {
  const storage = () => {
    const values: Record<string, string> = {}
    return new Proxy(values, { get: (target, key) => key === 'getItem' ? (k: string) => target[k] ?? null
      : key === 'setItem' ? (k: string, value: string) => { target[k] = value }
      : key === 'removeItem' ? (k: string) => { delete target[k] } : Reflect.get(target, key) })
  }
  vi.stubGlobal('localStorage', storage()); vi.stubGlobal('sessionStorage', storage())
})
afterEach(() => { vi.unstubAllGlobals() })
it('keeps the current employee input and pending receipt across a session-check outage, but erases them on confirmed revocation', async () => {
  sessionStorage.setItem('ai-helpdesk.session-owner', 'alice:1')
  localStorage.setItem('helpdesk.pending-case', JSON.stringify({ message_id: 'stable-id', body: 'unsent' }))
  let failure = 0
  vi.stubGlobal('fetch', vi.fn(async () => {
    if (failure === 1) throw new TypeError('offline')
    return new Response(JSON.stringify({ actor: { actor_id: 'alice', instance: 'staff', auth_version: 1, roles: ['employee'] }, csrf_token: 'csrf' }), { status: failure === 2 ? 401 : 200 })
  }))
  render(<QueryClientProvider client={new QueryClient()}><SessionGate><input aria-label="draft" defaultValue="retained material" /></SessionGate></QueryClientProvider>)
  expect(await screen.findByLabelText('draft')).toHaveValue('retained material')
  failure = 1; fireEvent.focus(window)
  expect(await screen.findByRole('alert')).toHaveTextContent('已保留当前输入')
  expect(screen.getByLabelText('draft')).toHaveValue('retained material')
  expect(localStorage.getItem('helpdesk.pending-case')).toContain('stable-id')
  failure = 0; fireEvent.focus(window)
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(localStorage.getItem('helpdesk.pending-case')).toContain('stable-id')
  failure = 2; fireEvent.focus(window)
  expect(await screen.findByRole('button', { name: /^登录$/ })).toBeInTheDocument()
  expect(localStorage.getItem('helpdesk.pending-case')).toBeNull()
  expect(screen.queryByLabelText('draft')).not.toBeInTheDocument()
})

it.each([200, 401])('ignores a stale Alice check with status %s after Bob is confirmed', async status => {
  sessionStorage.setItem('ai-helpdesk.session-owner', 'alice:1')
  const value = (actor_id: string) => new Response(JSON.stringify({ actor: { actor_id, instance: 'staff', auth_version: 1, roles: ['employee'] }, csrf_token: actor_id }), { status: 200 })
  let pending: ((response: Response) => void) | undefined
  let count = 0
  vi.stubGlobal('fetch', vi.fn(() => {
    count++
    if (count === 2) return new Promise<Response>(resolve => { pending = resolve })
    return Promise.resolve(value(count === 1 ? 'alice' : 'bob'))
  }))
  render(<QueryClientProvider client={new QueryClient()}><SessionGate><input aria-label="draft" /></SessionGate></QueryClientProvider>)
  await screen.findByText(/alice · employee/)
  sessionStorage.setItem('helpdesk.pending-case', 'alice-private-draft')
  fireEvent.focus(window) // delayed Alice response
  fireEvent.focus(window) // another tab changed the shared cookie to Bob
  await screen.findByText(/bob · employee/)
  expect(sessionStorage.getItem('helpdesk.pending-case')).toBeNull()
  sessionStorage.setItem('helpdesk.pending-case', 'bob-private-draft')
  pending!(status === 200 ? value('alice') : new Response('{}', { status: 401 }))
  await waitFor(() => expect(screen.queryByText(/alice · employee/)).not.toBeInTheDocument())
  expect(sessionStorage.getItem('helpdesk.pending-case')).toBe('bob-private-draft')
})

it('preserves the employee draft on a SQL service credential failure but clears it on actual employee expiry', async () => {
  const { sqlApi } = await import('@/api/sql/client')
  let status = 502
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/auth/session')
    ? new Response(JSON.stringify({ actor: { actor_id: 'alice', instance: 'staff', auth_version: 1, roles: ['employee'] }, csrf_token: 'csrf' }))
    : new Response(JSON.stringify({ detail: 'service unavailable' }), { status })))
  render(<QueryClientProvider client={new QueryClient()}><SessionGate><input aria-label="draft" defaultValue="retained material" /></SessionGate></QueryClientProvider>)
  await screen.findByLabelText('draft')
  sessionStorage.setItem('helpdesk.pending-case', 'pending-receipt')
  await expect(sqlApi.whoami()).rejects.toBeDefined()
  expect(screen.getByLabelText('draft')).toHaveValue('retained material')
  expect(sessionStorage.getItem('helpdesk.pending-case')).toBe('pending-receipt')
  status = 401
  await expect(sqlApi.whoami()).rejects.toBeDefined()
  await screen.findByRole('button', { name: /^登录$/ })
  expect(sessionStorage.getItem('helpdesk.pending-case')).toBeNull()
  expect(screen.queryByLabelText('draft')).not.toBeInTheDocument()
})

it.each(['request', 'attachment', 'chat', 'run', 'sql', 'sql-stream'])('ignores an old employee 401 from %s after identity changes', async kind => {
  const { api, attachmentApi, setSessionIdentity, streamChat, streamRun } = await import('@/api/client')
  const { sqlApi, streamQuery } = await import('@/api/sql/client')
  setSessionIdentity('alice:1')
  let finish: ((response: Response) => void) | undefined
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
  const expired = vi.fn()
  window.addEventListener('employee-session-expired', expired)
  const old = (kind === 'request' ? api.health('/api')
    : kind === 'attachment' ? attachmentApi.content('/api', 'old')
    : kind === 'chat' ? streamChat('/api', { message: 'old', user_id: 'alice' }).next()
    : kind === 'run' ? streamRun('/api', 'old').next()
    : kind === 'sql' ? sqlApi.history()
    : streamQuery('old', { userId: 'alice', sessionId: 'old' })).catch(() => null)
  await waitFor(() => expect(finish).toBeDefined())
  setSessionIdentity('bob:1')
  sessionStorage.setItem('helpdesk.pending-case', 'bob-receipt')
  finish!(new Response('{}', { status: 401 }))
  await old
  expect(expired).not.toHaveBeenCalled()
  expect(sessionStorage.getItem('helpdesk.pending-case')).toBe('bob-receipt')
  window.removeEventListener('employee-session-expired', expired)
  setSessionIdentity('')
})
