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

test('sessions.list verification still exposes all four real validators', async () => {
  const validators = await loadContractValidators('sessions.list')
  assert.deepEqual(Object.keys(validators).sort(), [
    'validateSessionsListParams', 'validateSessionsListRequestFrame',
    'validateSessionsListResponseFrame', 'validateSessionsListResult',
  ])
  assert.equal(validators.validateSessionsListParams({}), true)
  assert.equal(validators.validateSessionsListResult({ sessions: [], count: 0, ts: 1 }), true)
  assert.equal(validators.validateSessionsListResult({ sessions: [] }), false)
})

test('flow update verification preserves bounded identity and recovery control', async () => {
  const validators = await loadContractValidators('transport.flow.update')
  assert.deepEqual(Object.keys(validators).sort(), [
    'validateTransportFlowUpdateParams', 'validateTransportFlowUpdateRequestFrame',
    'validateTransportFlowUpdateResponseFrame', 'validateTransportFlowUpdateResult',
  ])
  const base = { delivery_epoch: 'synthetic-connection', ack_delivery_id: 0 }
  const resume = {
    key: 'webchat:synthetic', sync_revision: 'revision', stream_generation: 'generation', stream_seq: 0,
  }
  const valid = { ...base, staged_delivery_ids: [1], resume: [resume] }
  const original = structuredClone(valid)
  assert.equal(validators.validateTransportFlowUpdateParams(valid), true)
  assert.deepEqual(valid, original)
  for (const invalid of [
    { ...base, delivery_epoch: '' },
    { ...base, ack_delivery_id: -1 },
    { ...base, staged_delivery_ids: [1, 2] },
    { ...base, resume: [resume, { ...resume, key: 'webchat:other' }] },
    { ...base, unexpected: true },
  ]) assert.equal(validators.validateTransportFlowUpdateParams(invalid), false)
})

test('snapshot segment and dirty event verification retain complete role coverage', async () => {
  const snapshot = await loadContractValidators('sessions.messages.snapshot.read')
  assert.deepEqual(Object.keys(snapshot).sort(), [
    'validateSessionsMessagesSnapshotReadParams', 'validateSessionsMessagesSnapshotReadRequestFrame',
    'validateSessionsMessagesSnapshotReadResponseFrame', 'validateSessionsMessagesSnapshotReadResult',
  ])
  const params = {
    key: 'webchat:synthetic', sync_revision: 'revision', snapshot_id: 'snapshot', segment_index: 0,
  }
  assert.equal(snapshot.validateSessionsMessagesSnapshotReadParams(params), true)
  assert.equal(snapshot.validateSessionsMessagesSnapshotReadParams({ ...params, segment_index: 134 }), false)
  const dirty = await loadContractValidators('transport.flow.dirty', { kind: 'event' })
  assert.deepEqual(Object.keys(dirty), ['validateTransportFlowDirtyPayload'])
  assert.equal(dirty.validateTransportFlowDirtyPayload({
    delivery_epoch: 'synthetic-connection', dirty_keys: ['webchat:synthetic'], global_dirty: false,
  }), true)
  assert.equal(dirty.validateTransportFlowDirtyPayload({
    delivery_epoch: '', dirty_keys: [], global_dirty: false,
  }), false)
})

test('verification loading rejects unknown Contract identities', async () => {
  await assert.rejects(loadContractValidators('missing.method'), /unknown/)
})
