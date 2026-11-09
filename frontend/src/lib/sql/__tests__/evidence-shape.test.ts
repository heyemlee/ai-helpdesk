import { expect, it } from 'vitest'
import { normalizeResult } from '../model'

const valid = { database: 'erp_db', success: true, truncated: false, row_count: 1, rows: [{ amount: 150 }] }
it.each([
  { ...valid, row_count: undefined }, { ...valid, success: 'true' }, { ...valid, rows: [null] },
  { ...valid, truncated: true }, { ...valid, truncated: true, row_limit: 0 }, { ...valid, row_count: 2 },
  [], null,
])('marks malformed execution metadata as requiring review: %j', step => {
  const result = normalizeResult({ route: 'database_query', execution_status: 'success',
    queried_database: true, has_risk: false, degraded: false, steps: [step] })
  expect(result.hasRisk).toBe(true)
})
