import { expect, it } from 'vitest'
import { render, screen } from '@/test/render'
import { EvidenceDetails } from '../EvidenceDetails'
import { SafeMarkdown } from '../SafeMarkdown'

it('links short row references to literal values, units, time and expandable trace', () => {
  const cell = { database: 'erp_db', step_index: 0, row_index: 0, query_hash: 'abc', evidence_id: 'ev_stable',
    entity_keys: { order_ref: 'SO-Ab' }, field: 'amount', value: 150, unit: 'USD', as_of: '2020-01-01T00:00:00Z' }
  render(<EvidenceDetails records={[{ source: 'business_query', evidence: [cell,
    { ...cell, field: 'status', unit: null, value: '![outside](https://outside.example/img)' }] }]} />, { lang: 'en' })
  expect(screen.getByText('[1] Order: SO-Ab')).toBeInTheDocument()
  expect(screen.getByRole('cell', { name: '150', hidden: true })).toBeInTheDocument()
  expect(screen.getByRole('cell', { name: 'USD', hidden: true })).toBeInTheDocument()
  expect(screen.getByText('Full trace details')).toBeInTheDocument()
  expect(document.querySelector('img')).toBeNull()
  expect(document.querySelector('a')).toBeNull()
})

it('never loads policy images and preserves the server assigned image reference', () => {
  render(<><SafeMarkdown>{'> ![untrusted image](https://outside.example/x)'}</SafeMarkdown>
    <EvidenceDetails records={[{ source: 'employee_material', field: 'order_ref', value: 'claimed' },
      { source: 'verified_image', field: 'quantity', db_value: '2', unit: 'pcs', reply_reference: 'I1', business_source: { as_of: '2026-11-06T00:00:00Z' } },
      { source: 'verified_image', field: 'status', db_value: 'stale' }]} /></>)
  expect(document.querySelector('img')).toBeNull()
  expect(screen.getByText('[I1] 数量')).toBeInTheDocument()
  expect(screen.queryByText('[I2] 状态')).toBeNull()
})
