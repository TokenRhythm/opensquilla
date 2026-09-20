import assert from 'node:assert/strict'
import { createSessionRecoveryEvidence } from './session-recovery-rpc-evidence.mjs'

const KEY = 'synthetic:recovery'
const PREFIX = 'sessions.messages.'
function trace({ hydrate = true, release = true, revision = 'replacement', unsubscribe = false } = {}) {
  let clock = 0
  let request = 0
  const evidence = createSessionRecoveryEvidence(KEY, () => clock++)
  function rpc(method, params = {}, payload = {}, error, socket = 0) {
    const id = `request-${++request}`
    evidence.request(socket, { type: 'req', id, method: PREFIX + method,
      params: { key: KEY, lease_token: 'SECRET-LEASE', ...params } })
    evidence.response(socket, { type: 'res', id, ok: !error,
      payload: { ...params, ...payload, lease_token: 'SECRET-LEASE', messages: ['SECRET-MESSAGE'] }, error })
  }
  evidence.request(0, { type: 'req', id: 'auth', method: 'connect', params: { token: 'SECRET-AUTH' } })
  evidence.request(0, { type: 'req', id: 'other', method: PREFIX + 'subscribe', params: { key: 'another-session' } })
  evidence.request(0, { type: 'req', id: 'lost', method: PREFIX + 'subscribe', params: { key: KEY } }, true)
  const after = evidence.mark('fault-released')
  rpc('subscribe', {}, { hydration_complete: false })
  rpc('resume', { sync_revision: 'original', snapshot_id: 'old' }, {}, {
    code: 'SNAPSHOT_STALE', message: 'SECRET-ERROR', retryable: false,
  })
  if (release) rpc('snapshot.release', { sync_revision: 'original', snapshot_id: 'old' }, { retired: true })
  rpc('snapshot.read', { sync_revision: revision }, { snapshot_id: 'new', data: 'SECRET-SNAPSHOT' })
  rpc('resume', { sync_revision: revision, snapshot_id: 'new' })
  if (hydrate) rpc('hydrate', {}, { hydration_complete: true })
  if (unsubscribe) rpc('unsubscribe')
  return { evidence, after, rpc }
}

const recovered = trace()
assert.deepEqual(recovered.evidence.assertRecovered(recovered.after), {
  rejectedRevision: 'original', installedRevision: 'replacement', installedSnapshot: 'new', metadataRecovered: true,
})
const serialized = JSON.stringify(recovered.evidence.snapshot())
assert.doesNotMatch(serialized, /SECRET|lease_token|another-session|"connect"|"data"|"messages"/)
assert.equal(recovered.evidence.snapshot().events.filter(event => event.held).length, 1)

for (const [options, message] of [
  [{ hydrate: false }, /metadata must recover/],
  [{ release: false }, /retire the rejected snapshot/],
  [{ revision: 'original' }, /replacement snapshot with a new revision/],
  [{ unsubscribe: true }, /retain the target subscription lease/],
]) {
  const { evidence, after } = trace(options)
  assert.throws(() => evidence.assertRecovered(after), message)
}

const lateMetadata = trace({ hydrate: false })
assert.equal(lateMetadata.evidence.metadataRecovered(lateMetadata.after), false)
lateMetadata.rpc('hydrate', {}, { hydration_complete: false })
assert.equal(lateMetadata.evidence.metadataRecovered(lateMetadata.after), false)
lateMetadata.rpc('hydrate', {}, { hydration_complete: true }, undefined, 1)
assert.equal(lateMetadata.evidence.metadataRecovered(lateMetadata.after), false,
  'a different socket cannot satisfy the recovered subscription')
lateMetadata.rpc('hydrate', {}, { hydration_complete: true })
assert.equal(lateMetadata.evidence.metadataRecovered(lateMetadata.after), true)

const wrongSocket = createSessionRecoveryEvidence(KEY)
wrongSocket.request(0, { type: 'req', id: 'same-id', method: PREFIX + 'subscribe', params: { key: KEY } })
wrongSocket.response(1, { type: 'res', id: 'same-id', ok: true, payload: { hydration_complete: true } })
assert.equal(wrongSocket.metadataRecovered(0), false)
wrongSocket.response(0, { type: 'res', id: 'same-id', ok: true, payload: { hydration_complete: true } })
assert.equal(wrongSocket.metadataRecovered(0), true)

for (let index = 0; index < 1_001; index++) recovered.evidence.mark('bounded-diagnostic')
assert.equal(recovered.evidence.snapshot().events.length, 1_000)
assert.throws(() => recovered.evidence.assertRecovered(0), /evidence must be complete/)
console.log('Session recovery RPC evidence contracts passed (10 cases)')
