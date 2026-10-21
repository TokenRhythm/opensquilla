import { describe, expect, it } from 'vitest'
import { createPendingQueuePolicy, pendingQueueHandoffStorageKey, pendingQueuePolicyStorageKey,
  pendingQueueResumeStorageKey } from './pendingQueuePolicy'

const scope = { sessionKey: 'synthetic-session', deliveryIdentity: 'synthetic-gateway-subject' }

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
}

describe('pending queue Stop policy', () => {
  it('invalidates an already prepared drain and stays paused after remount', () => {
    const storage = memoryStorage()
    const first = createPendingQueuePolicy(storage)
    const prepared = first.capture(scope)
    expect(first.allows(prepared)).toBe(true)
    first.pause(scope)
    expect(first.allows(prepared)).toBe(false)
    const remounted = createPendingQueuePolicy(storage)
    expect(remounted.read(scope)).toMatchObject({ paused: true, persisted: true })
    expect(remounted.allows(remounted.capture(scope))).toBe(false)
  })

  it('checks another tab Stop without relying on broadcast notifications', () => {
    const storage = memoryStorage()
    const first = createPendingQueuePolicy(storage)
    const second = createPendingQueuePolicy(storage)
    const prepared = second.capture(scope)
    first.pause(scope)
    expect(second.allows(prepared)).toBe(false)
    expect(second.read(scope).paused).toBe(true)
  })

  it('allows an explicitly selected item without resuming the queue', () => {
    const policy = createPendingQueuePolicy(memoryStorage())
    policy.pause(scope)
    const selected = policy.capture(scope, true)
    expect(policy.allows(selected)).toBe(true)
    expect(policy.read(scope).paused).toBe(true)
    expect(policy.allows(policy.capture(scope))).toBe(false)
    policy.pause(scope)
    expect(policy.allows(selected)).toBe(false)
  })

  it('only a fresh permit can send after explicit resume', () => {
    const policy = createPendingQueuePolicy(memoryStorage())
    policy.pause(scope)
    const stale = policy.capture(scope)
    policy.resume(scope)
    expect(policy.allows(stale)).toBe(false)
    expect(policy.allows(policy.capture(scope))).toBe(true)
  })

  it('isolates chats and Gateway subjects, including ambiguous delimiters', () => {
    const policy = createPendingQueuePolicy(memoryStorage())
    policy.pause(scope)
    expect(policy.read({ ...scope, sessionKey: 'another-session' }).paused).toBe(false)
    expect(policy.read({ ...scope, deliveryIdentity: 'another-subject' }).paused).toBe(false)
    expect(pendingQueuePolicyStorageKey({ sessionKey: 'b:c', deliveryIdentity: 'a' }))
      .not.toBe(pendingQueuePolicyStorageKey({ sessionKey: 'c', deliveryIdentity: 'a:b' }))
  })

  it('keeps a failed Stop write paused instead of trusting the older stored value', () => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy(storage)
    policy.resume(scope)
    storage.setItem = () => { throw new Error('synthetic write failure') }
    expect(policy.pause(scope)).toMatchObject({ paused: true, persisted: false })
    expect(policy.read(scope)).toMatchObject({ paused: true, persisted: false })
    expect(policy.allows(policy.capture(scope))).toBe(false)
    expect(policy.resume(scope)).toMatchObject({ paused: true, persisted: false })
  })

  it('fails closed when shared policy cannot be read or is malformed', () => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy(storage)
    const prepared = policy.capture(scope)
    storage.values.set(pendingQueuePolicyStorageKey(scope), '{broken')
    expect(policy.allows(prepared)).toBe(false)
    expect(policy.read(scope)).toMatchObject({ paused: true, persisted: false })
    storage.getItem = () => { throw new Error('synthetic read failure') }
    expect(policy.allows(policy.capture(scope))).toBe(false)
  })

  it('a peer Stop still invalidates an explicit grant after a local write failure', () => {
    const storage = memoryStorage()
    const failedWriter = createPendingQueuePolicy({
      getItem: storage.getItem,
      setItem: () => { throw new Error('synthetic write failure') },
    })
    const peer = createPendingQueuePolicy(storage)
    failedWriter.pause(scope)
    const selected = failedWriter.capture(scope, true)
    expect(failedWriter.allows(selected)).toBe(true)
    peer.pause(scope)
    expect(failedWriter.allows(selected)).toBe(false)
    peer.resume(scope)
    expect(failedWriter.read(scope).paused).toBe(true)
  })

  it('uses instance-local memory for consumers without browser storage', () => {
    const policy = createPendingQueuePolicy(null)
    expect(policy.allows(policy.capture(scope))).toBe(true)
    policy.pause(scope)
    expect(policy.allows(policy.capture(scope))).toBe(false)
    expect(createPendingQueuePolicy(null).read(scope).paused).toBe(false)
    expect(policy.read({ ...scope, deliveryIdentity: '' }).paused).toBe(false)
  })

  it('holds the child before a handoff commits and reads a peer Stop after the wait', () => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy(storage)
    const peer = createPendingQueuePolicy(storage)
    const child = { ...scope, sessionKey: 'child' }
    const permit = policy.capture(child)
    expect(policy.beginHandoff(scope, child, 'fork-A').paused).toBe(true)
    expect(policy.allows(permit)).toBe(false)
    peer.pause(scope)
    expect(policy.completeHandoff(scope, child, 'fork-A').paused).toBe(true)
    expect(createPendingQueuePolicy(storage).read(child).paused).toBe(true)
    expect(policy.read(scope).paused).toBe(true)
  })

  it('recovers a pending handoff after reload and releases only its temporary pause', () => {
    const storage = memoryStorage()
    const child = { ...scope, sessionKey: 'child' }
    createPendingQueuePolicy(storage).beginHandoff(scope, child, 'fork-A')
    const reloaded = createPendingQueuePolicy(storage)
    expect(reloaded.read(child).paused).toBe(true)
    expect(reloaded.beginHandoff(scope, child, 'fork-A').paused).toBe(true)
    expect(reloaded.completeHandoff(scope, child, 'fork-A').paused).toBe(false)
  })

  it.each(['pause', 'resume'] as const)('preserves child %s across a late commit and repeated recovery', action => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy(storage)
    const child = { ...scope, sessionKey: 'child' }
    policy.pause(scope)
    policy.beginHandoff(scope, child, 'fork-A')
    const explicit = createPendingQueuePolicy(storage)[action](child)
    expect(policy.completeHandoff(scope, child, 'fork-A')).toMatchObject(explicit)
    const reloaded = createPendingQueuePolicy(storage)
    reloaded.beginHandoff(scope, child, 'fork-A')
    expect(reloaded.completeHandoff(scope, child, 'fork-A')).toMatchObject(explicit)
    expect(reloaded.read(scope).paused).toBe(true)
  })

  it.each([
    ['begin', 'pause'], ['begin', 'resume'],
    ['complete', 'pause'], ['complete', 'resume'],
  ] as const)('a peer child %s / %s wins even inside the automatic storage write', (phase, action) => {
    const storage = memoryStorage()
    const child = { ...scope, sessionKey: 'child' }
    let interleave = false
    let interrupted = false
    const shared = {
      getItem: storage.getItem,
      setItem: (key: string, value: string) => {
        if (interleave && key === pendingQueueHandoffStorageKey(child)) {
          interleave = false
          interrupted = true
          peer[action](child)
        }
        storage.setItem(key, value)
      },
    }
    const policy = createPendingQueuePolicy(shared)
    const peer = createPendingQueuePolicy(shared)
    if (action === 'resume') policy.pause(scope)
    if (phase === 'complete') policy.beginHandoff(scope, child, 'fork-A')
    interleave = true
    const result = phase === 'begin'
      ? policy.beginHandoff(scope, child, 'fork-A')
      : policy.completeHandoff(scope, child, 'fork-A')
    expect(interrupted).toBe(true)
    expect(result.paused).toBe(action === 'pause')
    const manual = peer.read(child)
    expect(manual.paused).toBe(action === 'pause')
    const reloaded = createPendingQueuePolicy(shared)
    reloaded.beginHandoff(scope, child, 'fork-A')
    reloaded.completeHandoff(scope, child, 'fork-A')
    expect(reloaded.read(child)).toEqual(manual)
  })

  it('reads the explicit decision after automatic metadata when checking a permit', () => {
    const storage = memoryStorage()
    const child = { ...scope, sessionKey: 'child' }
    let interleave = false
    const shared = {
      setItem: storage.setItem,
      getItem: (key: string) => {
        const value = storage.getItem(key)
        if (interleave && key === pendingQueueHandoffStorageKey(child)) {
          interleave = false
          peer.pause(child)
        }
        return value
      },
    }
    const policy = createPendingQueuePolicy(shared)
    const peer = createPendingQueuePolicy(shared)
    policy.beginHandoff(scope, child, 'fork-A')
    policy.completeHandoff(scope, child, 'fork-A')
    const permit = policy.capture(child)
    interleave = true
    expect(policy.allows(permit)).toBe(false)
  })

  it('persists a resume proof for exactly one paused generation without rewriting Stop', () => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy(storage)
    const paused = policy.pause(scope)
    const manual = storage.getItem(pendingQueuePolicyStorageKey(scope))
    const oldAutomatic = policy.capture(scope)
    const oldExplicit = policy.capture(scope, true)
    const resumed = policy.resumeGeneration(scope, paused.generation)!
    expect(resumed).toMatchObject({ paused: false, persisted: true })
    expect(resumed.generation).not.toBe(paused.generation)
    expect(policy.allows(oldAutomatic)).toBe(false)
    expect(policy.allows(oldExplicit)).toBe(false)
    expect(storage.getItem(pendingQueuePolicyStorageKey(scope))).toBe(manual)
    expect(createPendingQueuePolicy(storage).read(scope)).toEqual(resumed)
    expect(policy.resumeGeneration(scope, paused.generation)).toBeNull()
    policy.pause(scope)
    expect(storage.getItem(pendingQueueResumeStorageKey(scope, paused.generation))).toBeNull()
    expect(policy.read(scope).paused).toBe(true)
    expect(policy.resumeGeneration(scope, paused.generation)).toBeNull()
  })

  it('a peer Stop inside the resume proof write remains authoritative', () => {
    const storage = memoryStorage()
    const peer = createPendingQueuePolicy(storage)
    const paused = peer.pause(scope)
    const policy = createPendingQueuePolicy({
      getItem: storage.getItem,
      setItem: (key, value) => {
        peer.pause(scope)
        storage.setItem(key, value)
      },
    })
    const result = policy.resumeGeneration(scope, paused.generation)!
    expect(result.paused).toBe(true)
    expect(result.generation).not.toBe(paused.generation)
    expect(createPendingQueuePolicy(storage).read(scope)).toEqual(result)
  })

  it('an old proof write cannot overwrite a newer accepted resume', () => {
    const storage = memoryStorage()
    const peer = createPendingQueuePolicy(storage)
    const paused = peer.pause(scope)
    let newer: ReturnType<typeof peer.read> | undefined
    const policy = createPendingQueuePolicy({
      getItem: storage.getItem,
      setItem: (key, value) => {
        const stoppedAgain = peer.pause(scope)
        newer = peer.resumeGeneration(scope, stoppedAgain.generation)!
        storage.setItem(key, value)
      },
    })
    expect(policy.resumeGeneration(scope, paused.generation)).toEqual(newer)
    expect(policy.read(scope)).toMatchObject({ paused: false, persisted: true })
  })

  it('concurrent proofs for one Stop do not invalidate a permit issued after the first resume', () => {
    const storage = memoryStorage()
    const peer = createPendingQueuePolicy(storage)
    const paused = peer.pause(scope)
    let freshPermit: ReturnType<typeof peer.capture> | undefined
    const policy = createPendingQueuePolicy({
      getItem: storage.getItem,
      setItem: (key, value) => {
        peer.resumeGeneration(scope, paused.generation)
        freshPermit = peer.capture(scope)
        storage.setItem(key, value)
      },
    })
    policy.resumeGeneration(scope, paused.generation)
    expect(policy.allows(freshPermit!)).toBe(true)
  })

  it('does not let obsolete proof cleanup failure undo a new Stop', () => {
    const storage = memoryStorage()
    const policy = createPendingQueuePolicy({ ...storage,
      removeItem: () => { throw new Error('synthetic cleanup failure') } })
    const first = policy.pause(scope)
    policy.resumeGeneration(scope, first.generation)
    expect(policy.pause(scope)).toMatchObject({ paused: true, persisted: true })
    expect(policy.read(scope).paused).toBe(true)
  })

  it('a Stop during the proof read invalidates an already captured permit', () => {
    const storage = memoryStorage()
    const peer = createPendingQueuePolicy(storage)
    const paused = peer.pause(scope)
    peer.resumeGeneration(scope, paused.generation)
    let interleave = false
    const policy = createPendingQueuePolicy({
      getItem: key => {
        const value = storage.getItem(key)
        if (interleave && key === pendingQueueResumeStorageKey(scope, paused.generation)) {
          interleave = false
          peer.pause(scope)
        }
        return value
      },
      setItem: storage.setItem,
    })
    const permit = policy.capture(scope)
    interleave = true
    expect(policy.allows(permit)).toBe(false)
    expect(policy.read(scope).paused).toBe(true)
  })

  it('keeps the queue paused if a resume proof cannot be saved', () => {
    const storage = memoryStorage()
    const peer = createPendingQueuePolicy(storage)
    const paused = peer.pause(scope)
    const policy = createPendingQueuePolicy({ getItem: storage.getItem,
      setItem: () => { throw new Error('synthetic quota failure') } })
    expect(policy.resumeGeneration(scope, paused.generation)).toMatchObject({ paused: true, persisted: false })
    expect(createPendingQueuePolicy(storage).read(scope).paused).toBe(true)
    expect(policy.allows(policy.capture(scope))).toBe(false)
  })

  it('recognizes resume storage events only for the matching scope', () => {
    const policy = createPendingQueuePolicy(null)
    expect(policy.isScopeStorageKey(scope, pendingQueueResumeStorageKey(scope, 'paused-A'))).toBe(true)
    expect(policy.isScopeStorageKey(scope, pendingQueueResumeStorageKey(
      { ...scope, sessionKey: `${scope.sessionKey}-other` }, 'paused-A',
    ))).toBe(false)
    expect(policy.isScopeStorageKey(scope, pendingQueueResumeStorageKey(
      { ...scope, deliveryIdentity: 'another-account' }, 'paused-A',
    ))).toBe(false)
  })
})
