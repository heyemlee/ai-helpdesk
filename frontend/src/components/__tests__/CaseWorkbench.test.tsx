import { beforeEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, waitFor } from '@/test/render'
import { CaseWorkbench } from '../CaseWorkbench'
import type { CaseDetail } from '@/api/types'

let state: CaseDetail
let sent: { path: string; body: Record<string, unknown> }[]
let rejectInput: 'none' | 'conflict' | 'network'
const initial = (): CaseDetail => ({
  id: 'tk_case1', case_id: 'tk_case1', conv_id: 'conv-1', status: 'waiting_for_info', issue_type: 'missing_parts',
  customer_ref: 'Customer-A', order_refs: ['SO-1'], owner_actor_id: null, control_mode: 'assistant',
  version: 2, control_version: 1, facts: {}, pending_fields: ['model', 'quantity'],
  messages: [{ seq: 1, role: 'user', content: 'Missing doors', message_id: 'm1', run_id: 'r1', version: 1 }],
  drafts: [{ id: 'd1', body: 'Please confirm the model.', body_hash: 'hash1', version: 2, control_version: 1,
    status: 'pending', valid: true, invalid_reason: '', approved_by: null, evidence: [{ source: 'employee_material', value: 'SO-1' }] }],
  materials: [], runs: [{ run_id: 'r1', status: 'succeeded', applied: true, reason: '' }], events: [],
})
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => {
  state = initial(); sent = []; rejectInput = 'none'
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace('/api', '')
    if (path === '/tickets/cases') return response({ tickets: [state] })
    if (path === '/tickets/tk_case1/case') return response(state)
    const body = JSON.parse(String(init?.body ?? '{}'))
    sent.push({ path, body })
    if (path === '/tickets/cases/inputs') {
      if (rejectInput === 'conflict') {
        rejectInput = 'none'; state = { ...state, version: 5 }
        return response({ detail: { code: 'case_version_changed' } }, 409)
      }
      if (rejectInput === 'network') { rejectInput = 'none'; throw new TypeError('network down') }
      state = { ...state, version: state.version + 1, drafts: state.drafts.map(d => ({ ...d, valid: false, status: 'invalid' })) }
      return response({ case_id: state.id, run_id: 'r2', case_version: state.version, message_id: body.message_id }, 202)
    }
    if (path.endsWith('/approve')) {
      state = { ...state, drafts: state.drafts.map(d => ({ ...d, status: 'approved' })) }
    } else if (path.endsWith('/actions')) {
      state = { ...state, control_version: state.control_version + 1,
        control_mode: body.operation === 'takeover' ? 'human' : 'assistant',
        owner_actor_id: body.operation === 'takeover' ? 'employee-1' : null,
        status: body.operation === 'reopen' ? 'open' : state.status,
        drafts: state.drafts.map(d => ({ ...d, valid: false, status: 'invalid' })) }
    }
    return response(state)
  }))
})

async function show() {
  render(<CaseWorkbench base="/api" ticketId="tk_case1" />, { lang: 'en' })
  await screen.findByText('Missing doors', { exact: false })
  await screen.findByRole('button', { name: 'Save and continue' })
}

describe('Case employee workflow', () => {
  it('requires real employee sign-in and shows an actionable message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'employee_identity_required' }, 401)))
    render(<CaseWorkbench base="/api" />, { lang: 'en' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in with your company employee account')
    expect(screen.queryByRole('button', { name: 'Save and continue' })).not.toBeInTheDocument()
  })

  it('keeps input after a version conflict and resubmits with the refreshed version', async () => {
    await show()
    await userEvent.type(screen.getByLabelText('Message'), 'Model M-2, two missing')
    rejectInput = 'conflict'
    await userEvent.click(screen.getByRole('button', { name: 'Save and continue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your input is retained')
    expect(screen.getByLabelText('Message')).toHaveValue('Model M-2, two missing')
    await userEvent.click(screen.getByRole('button', { name: 'Save and continue' }))
    await waitFor(() => expect(sent.filter(s => s.path.endsWith('/inputs'))).toHaveLength(2))
    const [first, second] = sent.filter(s => s.path.endsWith('/inputs')).map(s => s.body)
    expect(first!.expected_version).toBe(2); expect(second!.expected_version).toBe(5)
    expect(second!.expected_control_version).toBe(1)
    expect(second!.case_id).toBe('tk_case1')
    expect(second).not.toHaveProperty('user_id')
    expect(await screen.findByText(/Invalidated/)).toBeInTheDocument()
  })

  it('reuses the exact message ID and body after an uncertain network result', async () => {
    await show()
    await userEvent.type(screen.getByLabelText('Message'), 'Supplement')
    rejectInput = 'network'
    await userEvent.click(screen.getByRole('button', { name: 'Save and continue' }))
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Save and continue' }))
    await waitFor(() => expect(sent.filter(s => s.path.endsWith('/inputs'))).toHaveLength(2))
    const [first, second] = sent.filter(s => s.path.endsWith('/inputs'))
    expect(second!.body).toEqual(first!.body)
  })

  it('binds approval to draft hash and versions; takeover invalidates approval', async () => {
    await show()
    await userEvent.click(screen.getByRole('button', { name: 'Approve this version' }))
    expect(await screen.findByText('Approved', { exact: false })).toBeInTheDocument()
    expect(sent[0]!.body).toEqual({ expected_version: 2, expected_control_version: 1, body_hash: 'hash1' })
    await userEvent.click(screen.getByRole('button', { name: 'Take over' }))
    expect(await screen.findByRole('button', { name: 'Release to assistant' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve this version' })).toBeDisabled()
    expect(sent[1]!.body.operation).toBe('takeover')
  })

  it('requires a reason to reopen a closed case', async () => {
    state = { ...state, status: 'resolved', drafts: state.drafts.map(d => ({ ...d, valid: false })) }
    render(<CaseWorkbench base="/api" ticketId="tk_case1" />, { lang: 'en' })
    const button = await screen.findByRole('button', { name: 'Reopen' })
    expect(button).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Reason to reopen'), 'New photograph confirmed the issue')
    await userEvent.click(button)
    await screen.findByRole('button', { name: 'Save and continue' })
    expect(sent[0]!.body).toMatchObject({ operation: 'reopen', reason: 'New photograph confirmed the issue' })
  })

  it('creates an explicit new issue instead of merging by order', async () => {
    render(<CaseWorkbench base="/api" />, { lang: 'en' })
    await screen.findByRole('button', { name: 'Save and continue' })
    await userEvent.selectOptions(screen.getByLabelText('Issue type'), 'wrong_item')
    await userEvent.type(screen.getByLabelText('Order reference'), 'SO-1')
    await userEvent.type(screen.getByLabelText('Message'), 'Wrong item on the same order')
    await userEvent.click(screen.getByRole('button', { name: 'Save and continue' }))
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]!.body).toMatchObject({ new_case: true, issue_type: 'wrong_item', facts: { order_ref: 'SO-1' } })
    expect(sent[0]!.body).not.toHaveProperty('case_id')
  })
})
