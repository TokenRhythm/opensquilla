import assert from 'node:assert/strict'

const SUBSCRIBE = 'sessions.messages.subscribe'
const HYDRATE = 'sessions.messages.hydrate'
const READ = 'sessions.messages.snapshot.read'
const RESUME = 'sessions.messages.resume'
const RELEASE = 'sessions.messages.snapshot.release'
const UNSUBSCRIBE = 'sessions.messages.unsubscribe'
const METHODS = new Set([SUBSCRIBE, HYDRATE, READ, RESUME, RELEASE, UNSUBSCRIBE, 'chat.history'])

// Keep diagnostic evidence limited to the synthetic target's RPC control flow.
// Never retain connection credentials, lease tokens, messages or snapshot data.
export function createSessionRecoveryEvidence(sessionKey, now = Date.now) {
  const startedAt = now()
  const pending = new Map()
  const events = []
  let sequence = 0
  let requestId = 0
  let overflow = false
  const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value)
    ? value : undefined
  function append(event) {
    const item = { sequence: ++sequence, elapsedMs: now() - startedAt, ...event }
    if (events.length === 1_000) {
      overflow = true
      events.shift()
    }
    events.push(item)
    return item.sequence
  }
  function request(socket, frame, held = false) {
    if (frame?.type !== 'req' || !METHODS.has(frame.method)
      || (frame.params?.key ?? frame.params?.sessionKey) !== sessionKey) return
    const item = {
      socket, requestId: ++requestId, method: frame.method,
      revision: identifier(frame.params?.sync_revision),
      snapshot: identifier(frame.params?.snapshot_id),
    }
    item.requestSequence = append({ direction: 'request', ...item, held })
    if (!held) pending.set(`${socket}:${frame.id}`, item)
  }
  function response(socket, frame) {
    if (frame?.type !== 'res') return
    const key = `${socket}:${frame.id}`
    const item = pending.get(key)
    if (!item) return
    pending.delete(key)
    append({
      direction: 'response', ...item, ok: frame.ok === true,
      revision: identifier(frame.payload?.sync_revision) ?? item.revision,
      snapshot: identifier(frame.payload?.snapshot_id) ?? item.snapshot,
      ...(typeof frame.payload?.hydration_complete === 'boolean'
        ? { hydrationComplete: frame.payload.hydration_complete } : {}),
      ...(frame.ok !== true ? { errorCode: identifier(frame.error?.code) } : {}),
    })
  }
  function metadataRecovered(after) {
    return events.some(ack => ack.sequence > after && ack.direction === 'response'
      && ack.method === SUBSCRIBE && ack.ok && (ack.hydrationComplete === true
        || events.some(hydrate => hydrate.requestSequence > ack.sequence
          && hydrate.socket === ack.socket && hydrate.direction === 'response'
          && hydrate.method === HYDRATE && hydrate.ok && hydrate.hydrationComplete === true)))
  }
  function assertRecovered(after) {
    assert.equal(overflow, false, 'recovery RPC evidence must be complete')
    const recovery = events.filter(item => item.sequence > after)
    const stale = recovery.find(item => item.direction === 'response' && item.method === RESUME
      && !item.ok && ['SNAPSHOT_STALE', 'SNAPSHOT_EXPIRED'].includes(item.errorCode))
    assert.ok(stale?.revision, 'recovery must exercise rejection of the original snapshot proof')
    const fresh = recovery.find(item => item.direction === 'response' && item.method === READ
      && item.ok && item.sequence > stale.sequence && item.revision && item.revision !== stale.revision)
    assert.ok(fresh?.snapshot, 'automatic recovery must read a replacement snapshot with a new revision')
    const installed = recovery.find(item => item.direction === 'response' && item.method === RESUME
      && item.ok && item.sequence > fresh.sequence && item.revision === fresh.revision
      && item.snapshot === fresh.snapshot && item.socket === fresh.socket)
    assert.ok(installed, 'automatic recovery must confirm the replacement snapshot')
    assert.ok(recovery.some(item => item.direction === 'response' && item.method === RELEASE
      && item.ok && item.revision === stale.revision), 'recovery must retire the rejected snapshot')
    assert.equal(recovery.some(item => item.method === UNSUBSCRIBE), false,
      'automatic recovery must retain the target subscription lease')
    assert.equal(metadataRecovered(after), true, 'metadata must recover from a new subscription ACK')
    return { rejectedRevision: stale.revision, installedRevision: installed.revision,
      installedSnapshot: installed.snapshot, metadataRecovered: true }
  }
  return {
    request, response, metadataRecovered, assertRecovered,
    mark: stage => append({ stage }),
    snapshot: () => ({ overflow, events: events.map(item => ({ ...item })) }),
  }
}
