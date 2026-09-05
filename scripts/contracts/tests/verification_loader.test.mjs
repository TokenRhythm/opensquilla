import assert from 'node:assert/strict'
import test from 'node:test'
import { loadContractValidators } from '../gateway_contract_verification.mjs'

test('temporary verification keeps request and response validators absent from production', async () => {
  const validators = await loadContractValidators('sessions.resolve')
  assert.equal(typeof validators.validateSessionsResolveRequestFrame, 'function')
  assert.equal(typeof validators.validateSessionsResolveResponseFrame, 'function')
  assert.equal(validators.validateSessionsResolveRequestFrame({
    type: 'req', id: 'synthetic', method: 'sessions.resolve', params: { key: 'webchat:synthetic' },
  }), true)
  assert.equal(validators.validateSessionsResolveRequestFrame({ type: 'req', method: 'wrong' }), false)
})

test('sessions.list supplemental roles are real validators, not claimed old exports', async () => {
  const validators = await loadContractValidators('sessions.list')
  assert.deepEqual(Object.keys(validators).sort(), [
    'validateSessionsListParams', 'validateSessionsListRequestFrame',
    'validateSessionsListResponseFrame', 'validateSessionsListResult',
  ])
  assert.equal(validators.validateSessionsListParams({}), true)
  assert.equal(validators.validateSessionsListResult({ sessions: [], count: 0, ts: 1 }), true)
  assert.equal(validators.validateSessionsListResult({ sessions: [] }), false)
})

test('verification loading rejects unknown Contract identities', async () => {
  await assert.rejects(loadContractValidators('missing.method'), /unknown/)
})
