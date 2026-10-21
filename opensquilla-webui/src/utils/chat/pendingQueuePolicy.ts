import { createClientRequestId } from './messageIdentity'

/** Browser-local delivery policy, scoped to the Gateway subject and chat. */
export interface PendingQueueScope {
  sessionKey: string
  deliveryIdentity: string
}

export interface PendingQueuePolicyState {
  readonly generation: string
  readonly paused: boolean
  readonly persisted: boolean
}

export interface PendingQueueDeliveryPermit {
  readonly scope: Readonly<PendingQueueScope>
  readonly generation: string
  readonly explicit: boolean
}

type PolicyStorage = Pick<Storage, 'getItem' | 'setItem'> & Partial<Pick<Storage, 'removeItem'>>
interface StoredPolicyState extends PendingQueuePolicyState {
  handoff?: { sourceSessionKey: string; ownerRequestId: string; completed: boolean }
}
const STORAGE_PREFIX = 'opensquilla.pending-queue-policy.v1:'
const HANDOFF_STORAGE_PREFIX = 'opensquilla.pending-queue-handoff.v1:'
const RESUME_STORAGE_PREFIX = 'opensquilla.pending-queue-resume.v1:'
function browserStorage(): PolicyStorage | null {
  try { return globalThis.localStorage ?? null } catch {
    // An unavailable browser storage API differs from a non-browser consumer.
    // Keep a browser permission failure fail-closed instead of using a fresh
    // in-memory policy that forgets a previously persisted Stop.
    return {
      getItem: () => { throw new Error('Queue policy storage is unavailable') },
      setItem: () => { throw new Error('Queue policy storage is unavailable') },
    }
  }
}

function memoryStorage(): PolicyStorage {
  const values = new Map<string, string>()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) },
  }
}

export function pendingQueuePolicyStorageKey(scope: PendingQueueScope): string {
  return STORAGE_PREFIX + JSON.stringify([scope.deliveryIdentity, scope.sessionKey])
}

export function pendingQueueHandoffStorageKey(scope: PendingQueueScope): string {
  return HANDOFF_STORAGE_PREFIX + JSON.stringify([scope.deliveryIdentity, scope.sessionKey])
}

export function pendingQueueResumeStorageKey(scope: PendingQueueScope, generation: string): string {
  return RESUME_STORAGE_PREFIX + JSON.stringify([scope.deliveryIdentity, scope.sessionKey, generation])
}

/**
 * Every guard re-reads synchronous shared storage, so a missed storage event or
 * BroadcastChannel message cannot authorize another tab's stale drain. This
 * does not coordinate browsers on other devices or already-submitted turns.
 */
export function createPendingQueuePolicy(storage: PolicyStorage | null = browserStorage()) {
  const sharedStorage = storage ?? memoryStorage()
  const writeFailures = new Map<string, PendingQueuePolicyState>()
  const readFailures = new Map<string, PendingQueuePolicyState>()
  const initial: PendingQueuePolicyState = Object.freeze({
    generation: 'initial', paused: false, persisted: true,
  })

  function failedRead(key: string): PendingQueuePolicyState {
    let state = readFailures.get(key)
    if (!state) {
      state = Object.freeze({ generation: createClientRequestId(), paused: true, persisted: false })
      readFailures.set(key, state)
    }
    return state
  }

  function read(scope: PendingQueueScope): PendingQueuePolicyState {
    const state = readBase(scope)
    if (!state.paused || !state.persisted) return state
    const resumed = readWithFailures(pendingQueueResumeStorageKey(scope, state.generation))
    // A peer Stop can arrive while reading the proof. Its newer generation
    // must win before this read authorizes a queued delivery.
    const current = readBase(scope)
    if (current.generation !== state.generation) return current
    if (!resumed.persisted) return resumed
    return resumed.generation !== 'initial' && !resumed.paused
      ? { generation: JSON.stringify([state.generation, resumed.generation]), paused: false, persisted: true }
      : state
  }

  function readBase(scope: PendingQueueScope): PendingQueuePolicyState {
    const key = pendingQueuePolicyStorageKey(scope)
    if (!scope.sessionKey) return failedRead(key)
    const inherited = readWithFailures(pendingQueueHandoffStorageKey(scope))
    // Read the decisive manual record last, after any automatic metadata read.
    const manual = readWithFailures(key)
    // Separate keys make explicit Stop/Resume win over an automatic write
    // already in progress in another tab. localStorage has no CAS operation.
    return manual.generation !== 'initial' ? manual : inherited
  }

  function readWithFailures(key: string): StoredPolicyState {
    const shared = readShared(key)
    // A failed Stop write must remain authoritative in this tab even if the
    // previous shared value allows delivery. Still read peers' decisions so
    // another Stop invalidates an explicitly selected item in this tab.
    const failedWrite = writeFailures.get(key)
    return failedWrite ? {
      generation: JSON.stringify([failedWrite.generation, shared.generation]),
      paused: true,
      persisted: false,
    } : shared
  }

  function readShared(key: string): StoredPolicyState {
    try {
      const raw = sharedStorage.getItem(key)
      if (raw === null) {
        readFailures.delete(key)
        return initial
      }
      const value: unknown = JSON.parse(raw)
      if (!value || typeof value !== 'object') return failedRead(key)
      const state = value as Record<string, unknown>
      if (state.version !== 1 || typeof state.generation !== 'string' || !state.generation
        || typeof state.paused !== 'boolean') return failedRead(key)
      let handoff: StoredPolicyState['handoff']
      if (state.handoff !== undefined) {
        if (!state.handoff || typeof state.handoff !== 'object') return failedRead(key)
        const value = state.handoff as Record<string, unknown>
        if (typeof value.sourceSessionKey !== 'string' || !value.sourceSessionKey
          || typeof value.ownerRequestId !== 'string' || !value.ownerRequestId
          || typeof value.completed !== 'boolean'
          || (!value.completed && state.paused !== true)) return failedRead(key)
        handoff = { sourceSessionKey: value.sourceSessionKey, ownerRequestId: value.ownerRequestId,
          completed: value.completed }
      }
      readFailures.delete(key)
      return { generation: state.generation, paused: state.paused, persisted: true, handoff }
    } catch {
      return failedRead(key)
    }
  }

  function write(
    scope: PendingQueueScope,
    paused: boolean,
    handoff?: StoredPolicyState['handoff'],
  ): PendingQueuePolicyState {
    const key = handoff ? pendingQueueHandoffStorageKey(scope) : pendingQueuePolicyStorageKey(scope)
    const previousGeneration = handoff ? null : readBase(scope).generation
    const generation = createClientRequestId()
    const failed: PendingQueuePolicyState = Object.freeze({ generation, paused: true, persisted: false })
    // Publish a fail-closed local decision before trying shared persistence.
    writeFailures.set(key, failed)
    try {
      if (!scope.sessionKey) return read(scope)
      sharedStorage.setItem(key, JSON.stringify({ version: 1, generation, paused, handoff }))
      writeFailures.delete(key)
      readFailures.delete(key)
      if (previousGeneration && previousGeneration !== 'initial') {
        try { sharedStorage.removeItem?.(pendingQueueResumeStorageKey(scope, previousGeneration)) } catch {
          // Obsolete proofs are harmless; cleanup cannot invalidate the new decision.
        }
      }
      return read(scope)
    } catch {
      return read(scope)
    }
  }

  function beginHandoff(
    source: PendingQueueScope,
    target: PendingQueueScope,
    ownerRequestId: string,
  ): PendingQueuePolicyState {
    const state = read(target)
    // Replayed handoffs preserve an existing child policy, including explicit
    // decisions made after the first migration.
    if (source.sessionKey === target.sessionKey || !ownerRequestId
      || state.generation !== 'initial' || !state.persisted) return state
    return write(target, true, { sourceSessionKey: source.sessionKey, ownerRequestId, completed: false })
  }

  function completeHandoff(
    source: PendingQueueScope,
    target: PendingQueueScope,
    ownerRequestId: string,
  ): PendingQueuePolicyState {
    const manual = readWithFailures(pendingQueuePolicyStorageKey(target))
    if (manual.generation !== 'initial') return manual
    const state = readWithFailures(pendingQueueHandoffStorageKey(target))
    if (state.handoff?.ownerRequestId !== ownerRequestId
      || state.handoff.sourceSessionKey !== source.sessionKey
      || state.handoff.completed) return read(target)
    // Read after the async WAL commit: Stop may have arrived during the wait.
    // Explicit child decisions have their own key and always take precedence.
    // A crash before here leaves the child held.
    return write(target, read(source).paused, { ...state.handoff, completed: true })
  }

  function resumeGeneration(scope: PendingQueueScope, generation: string): PendingQueuePolicyState | null {
    const current = read(scope)
    if (!current.paused || !current.persisted || current.generation !== generation) return null
    const key = pendingQueueResumeStorageKey(scope, generation)
    const resumedGeneration = createClientRequestId()
    writeFailures.set(key, { generation: resumedGeneration, paused: true, persisted: false })
    try {
      // This proof cannot overwrite a concurrent Stop or a newer resume proof:
      // each paused generation has its own key and is never reused.
      sharedStorage.setItem(key, JSON.stringify({ version: 1, generation: 'resumed', paused: false }))
      writeFailures.delete(key)
      readFailures.delete(key)
    } catch {
      // read() keeps this generation paused when persistence failed.
    }
    return read(scope)
  }

  function capture(scope: PendingQueueScope, explicit = false): PendingQueueDeliveryPermit {
    return Object.freeze({
      scope: Object.freeze({ ...scope }), generation: read(scope).generation, explicit,
    })
  }

  function allows(permit: PendingQueueDeliveryPermit): boolean {
    const state = read(permit.scope)
    return state.generation === permit.generation && (permit.explicit || !state.paused)
  }

  return {
    read,
    pause: (scope: PendingQueueScope) => write(scope, true),
    resume: (scope: PendingQueueScope) => write(scope, false),
    capture,
    allows,
    beginHandoff,
    completeHandoff,
    resumeGeneration,
    storageKey: pendingQueuePolicyStorageKey,
    handoffStorageKey: pendingQueueHandoffStorageKey,
    isScopeStorageKey: (scope: PendingQueueScope, key: string) => (
      key === pendingQueuePolicyStorageKey(scope) || key === pendingQueueHandoffStorageKey(scope)
      || key.startsWith(RESUME_STORAGE_PREFIX
        + JSON.stringify([scope.deliveryIdentity, scope.sessionKey]).slice(0, -1) + ',')
    ),
  }
}
