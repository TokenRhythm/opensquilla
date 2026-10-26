import assert from 'node:assert/strict'
import test from 'node:test'
import { loadContractValidators } from '../gateway_contract_verification.mjs'

for (const [method, base, field] of [
  ['workbench.resources.get', { resource: {} }, 'resource'],
  ['workbench.resources.list', {
    resources: [{}], totalCount: 1, pageSize: 1, returnedCount: 1, hasMore: false, nextCursor: null,
  }, 'resources'],
  ['workbench.resources.open', {
    disposition: 'document', resolution: { status: 'current' }, resource: {}, materialized: false,
  }, 'resource'],
]) {
  test(`${method} validates optional source path without modifying identity`, async () => {
    const { validateResult } = await loadContractValidators(method)
    assert.equal(validateResult(base), true)
    for (const [sourcePath, valid] of [
      ['original/report.docx', true], ['', false], ['x'.repeat(4097), false], [123, false], [null, true],
    ]) {
      const value = structuredClone(base)
      const resource = field === 'resources' ? value[field][0] : value[field]
      resource.sourcePath = sourcePath
      const original = structuredClone(value)
      assert.equal(validateResult(value), valid)
      assert.deepEqual(value, original)
    }
  })
}
