import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref, shallowRef } from 'vue'
import { SessionReadContractError, SessionReadFailure, type SessionReadLease } from '@/modules/sessionReadLifecycle'
import { createSessionMetadataRecovery } from './createSessionMetadataRecovery'
import type { SessionMetadataOutcome, SessionMetadataRetryOptions, SessionMetadataState } from './useChatSessionSubscription'

const cleanups: (() => void)[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.useRealTimers() })

function harness(initialAuthority = 'owner-a') {
  const sessionKey = ref('alpha')
  const authority = ref(initialAuthority)
  const connection = ref('connected')
  const livePhase = ref('ready')
  const lease = shallowRef({} as SessionReadLease)
  const state = shallowRef<SessionMetadataState>({ phase: 'idle', key: 'alpha', lease: null, attempt: 0, outcome: null })
  let ordinal = 0
  type MetadataOperation = { promise: Promise<SessionMetadataOutcome>, finish(outcome: SessionMetadataOutcome): void }
  let active: MetadataOperation | null = null
  function begin(): MetadataOperation {
    const attempt = ++ordinal
    const capturedLease = lease.value
    let resolve!: (outcome: SessionMetadataOutcome) => void
    const promise = new Promise<SessionMetadataOutcome>(finish => { resolve = finish })
    const operation = {
      promise,
      finish(outcome: SessionMetadataOutcome) {
        if (active === operation) {
          active = null
          state.value = { phase: outcome.ok ? 'ready' : 'error', key: sessionKey.value,
            lease: capturedLease, attempt, outcome }
        }
        resolve(outcome)
      },
    }
    active = operation
    state.value = { phase: 'loading', key: sessionKey.value, lease: capturedLease, attempt, outcome: null }
    return operation
  }
  const cancelAttempt = vi.fn(() => active?.finish({ ok: false, cancelled: true }))
  const runAttempt = vi.fn((_options: SessionMetadataRetryOptions) => begin().promise)
  const lane = createSessionMetadataRecovery({
    sessionKey, authority: () => authority.value, state, connectionState: connection, livePhase,
    currentLease: () => lease.value, canRetry: () => livePhase.value === 'ready',
    runAttempt, cancelAttempt,
  })
  lane.activate()
  cleanups.push(() => lane.dispose())
  return { lane, state, sessionKey, authority, connection, livePhase, lease, begin, runAttempt, cancelAttempt,
    currentOperation: () => active,
    finish: (outcome: SessionMetadataOutcome) => active?.finish(outcome) }
}

describe('owned metadata recovery lane', () => {
  it('does not retry a contract failure merely because its text mentions a connection', async () => {
    const h = harness()
    h.begin().finish({ ok: false, error: new SessionReadContractError('Invalid connection metadata contract.') })
    expect(h.lane.terminal.value).toBe(true)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(h.runAttempt).not.toHaveBeenCalled()
  })
  it('publishes metadata success independently of an already-ready live subscription', async () => {
    const h = harness()
    const ready = h.lane.retryMetadata()
    expect(h.runAttempt).toHaveBeenCalledOnce()
    expect(h.livePhase.value).toBe('ready')
    h.finish({ ok: true })
    await expect(ready).resolves.toBe(true)
    expect(h.lane.phase.value).toBe('ready')
    expect(h.lane.terminal.value).toBe(false)
  })

  it('shares recovery while a short waiter and an aborted waiter leave independently', async () => {
    const h = harness()
    const controller = new AbortController()
    const short = h.lane.retryMetadata({ timeoutMs: 25 })
    const aborted = h.lane.retryMetadata({ signal: controller.signal })
    const owner = h.lane.retryMetadata()
    expect(h.runAttempt).toHaveBeenCalledOnce()
    controller.abort()
    await expect(aborted).resolves.toBe(false)
    await vi.advanceTimersByTimeAsync(25)
    await expect(short).resolves.toBe(false)
    expect(h.cancelAttempt).not.toHaveBeenCalled()
    h.finish({ ok: true })
    await expect(owner).resolves.toBe(true)
  })

  it('retains one deadline across read attempts, same-owner leases and connection flaps', async () => {
    const h = harness()
    h.begin()
    await vi.advanceTimersByTimeAsync(60_000)
    h.lease.value = {} as SessionReadLease
    h.begin()
    h.connection.value = 'disconnected'
    await vi.advanceTimersByTimeAsync(59_999)
    expect(h.lane.terminal.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(h.lane.terminal.value).toBe(true)
    expect(h.lane.error.value).toMatchObject({ kind: 'budget-exhausted' })
    h.connection.value = 'connected'
    await vi.advanceTimersByTimeAsync(20_000)
    expect(h.runAttempt).not.toHaveBeenCalled()
    expect(h.lane.canApplyMetadata()).toBe(false)
  })

  it('uses the original deadline after first unknown-to-proven authority binding', async () => {
    const h = harness('')
    h.begin()
    await vi.advanceTimersByTimeAsync(60_000)
    h.authority.value = 'owner-a'
    expect(h.cancelAttempt).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.lane.terminal.value).toBe(true)
    expect(h.lane.error.value).toMatchObject({ kind: 'budget-exhausted' })
  })

  it('honors RetryAfter across replacement reads and first authority binding', async () => {
    const h = harness('')
    h.begin().finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true, 60_000) })
    h.livePhase.value = 'connecting'
    await vi.advanceTimersByTimeAsync(20_000)
    h.authority.value = 'owner-a'
    h.lease.value = {} as SessionReadLease
    h.begin()
    let admitted = false
    const gate = h.lane.beforeMetadataRead(new AbortController().signal).then(() => { admitted = true })
    await vi.advanceTimersByTimeAsync(39_999)
    expect(admitted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await gate
    expect(admitted).toBe(true)
    expect(h.runAttempt).not.toHaveBeenCalled()
  })

  it('bounds a late attempt by remaining incident time', async () => {
    const h = harness()
    const initial = h.begin()
    await vi.advanceTimersByTimeAsync(119_000)
    initial.finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true) })
    await vi.advanceTimersByTimeAsync(500)
    expect(h.runAttempt).toHaveBeenCalledExactlyOnceWith({ timeoutMs: 500 })
    await vi.advanceTimersByTimeAsync(500)
    expect(h.lane.terminal.value).toBe(true)
    expect(h.cancelAttempt).toHaveBeenCalled()
  })

  it('does not turn an explicit retry into admission after a terminal contract failure', async () => {
    const h = harness()
    const failure = new SessionReadContractError('Hydration returned an incomplete contract')
    h.begin().finish({ ok: false, error: failure })
    expect(h.lane.terminal.value).toBe(true)
    expect(h.lane.explicitRetryAvailable.value).toBe(false)
    await expect(h.lane.retryMetadata()).resolves.toBe(false)
    expect(h.runAttempt).not.toHaveBeenCalled()
    await expect(h.lane.retryMetadata({ explicit: true })).resolves.toBe(false)
    expect(h.runAttempt).not.toHaveBeenCalled()
    expect(h.lane.error.value).toBe(failure)
  })

  it.each([
    ['key', 'budget'], ['authority', 'budget'],
    ['key', 'contract'], ['authority', 'contract'],
  ] as const)('restores retained terminal publication on %s revisit after %s failure', async (scope, failure) => {
    const h = harness()
    const initial = h.begin()
    if (failure === 'budget') await vi.advanceTimersByTimeAsync(120_000)
    else initial.finish({ ok: false, error: new SessionReadContractError('Invalid metadata') })
    expect(h.lane.terminal.value).toBe(true)
    const retainedError = h.lane.error.value
    const identity = scope === 'key' ? h.sessionKey : h.authority
    const original = identity.value
    identity.value = 'other'
    expect(h.lane.terminal.value).toBe(false)
    identity.value = original
    expect(h.lane.terminal.value).toBe(true)
    expect(h.lane.phase.value).toBe('error')
    expect(h.lane.error.value).toBe(retainedError)
    const cancellations = h.cancelAttempt.mock.calls.length
    for (let index = 0; index < 3; index++) {
      expect(h.lane.canApplyMetadata()).toBe(false)
      await expect(h.lane.retryMetadata()).resolves.toBe(false)
      await expect(h.lane.beforeMetadataRead(new AbortController().signal)).rejects.toBe(retainedError)
      expect(h.lane.phase.value).toBe('error')
      expect(h.lane.terminal.value).toBe(true)
    }
    expect(h.cancelAttempt).toHaveBeenCalledTimes(cancellations)
    expect(h.runAttempt).not.toHaveBeenCalled()
    expect(h.lane.explicitRetryAvailable.value).toBe(failure === 'budget')
    const retried = h.lane.retryMetadata({ explicit: true })
    if (failure === 'contract') {
      await expect(retried).resolves.toBe(false)
      expect(h.runAttempt).not.toHaveBeenCalled()
      expect(h.lane.terminal.value).toBe(true)
      return
    }
    expect(h.runAttempt).toHaveBeenCalledOnce()
    h.finish({ ok: true })
    await expect(retried).resolves.toBe(true)
    expect(h.lane.terminal.value).toBe(false)
    expect(h.lane.explicitRetryAvailable.value).toBe(false)
  })

  it.each([
    ['nonretryable', { ok: false, error: new SessionReadFailure('unavailable', 'Access denied', false) }],
    ['server budget', { ok: false, error: new SessionReadFailure('budget-exhausted', 'Server limit', false) }],
    ['missing session', { ok: false, sessionMissing: true,
      error: new SessionReadFailure('unavailable', 'Session no longer exists', true) }],
  ] as const)('does not renew %s rejection as if it were local exhaustion', async (_kind, outcome) => {
    const h = harness()
    h.begin().finish(outcome)
    await vi.advanceTimersByTimeAsync(180_000)
    expect(h.lane.explicitRetryAvailable.value).toBe(false)
    await expect(h.lane.retryMetadata({ explicit: true })).resolves.toBe(false)
    expect(h.runAttempt).not.toHaveBeenCalled()
    expect(h.lane.error.value).toBe(outcome.error)
  })

  it('joins repeated explicit clicks without extending the renewed deadline', async () => {
    const h = harness()
    h.begin()
    await vi.advanceTimersByTimeAsync(120_000)
    expect(h.lane.explicitRetryAvailable.value).toBe(true)
    const first = h.lane.retryMetadata({ explicit: true })
    expect(h.lane.explicitRetryAvailable.value).toBe(false)
    await vi.advanceTimersByTimeAsync(60_000)
    const repeated = h.lane.retryMetadata({ explicit: true })
    expect(h.runAttempt).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(59_999)
    expect(h.lane.terminal.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await expect(first).resolves.toBe(false)
    await expect(repeated).resolves.toBe(false)
    expect(h.lane.terminal.value).toBe(true)
    expect(h.lane.explicitRetryAvailable.value).toBe(true)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(h.runAttempt).toHaveBeenCalledOnce()
  })

  it('retains a long server wait across multiple bounded manual episodes', async () => {
    const h = harness()
    h.begin().finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true, 300_000) })
    await vi.advanceTimersByTimeAsync(120_000)
    const first = h.lane.retryMetadata({ explicit: true })
    await vi.advanceTimersByTimeAsync(120_000)
    await expect(first).resolves.toBe(false)
    expect(h.runAttempt).not.toHaveBeenCalled()
    const second = h.lane.retryMetadata({ explicit: true })
    await vi.advanceTimersByTimeAsync(59_999)
    expect(h.runAttempt).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(h.runAttempt).toHaveBeenCalledOnce()
    h.finish({ ok: true })
    await expect(second).resolves.toBe(true)
    expect(h.lane.phase.value).toBe('ready')
  })

  it('does not revive an exhausted admission gate when explicit retry renews synchronously', async () => {
    const h = harness()
    h.begin().finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true, 180_000) })
    const oldGate = h.lane.beforeMetadataRead(new AbortController().signal).then(
      () => 'admitted', (error: unknown) => error,
    )
    // Renew before the old gate continuation can observe the elapsed deadline.
    vi.advanceTimersByTime(120_000)
    const retry = h.lane.retryMetadata({ explicit: true })
    await expect(oldGate).resolves.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.runAttempt).toHaveBeenCalledOnce()
    h.finish({ ok: true })
    await expect(retry).resolves.toBe(true)
  })

  it('cannot certify renewed readiness from an exhausted attempt completion or publication', async () => {
    const h = harness()
    // Keep the old underlying promise unresolved even after cancellation. The
    // adapter still publishes cancellation, as the production adapter does.
    h.cancelAttempt.mockImplementation(() => {
      h.state.value = { ...h.state.value, phase: 'error', outcome: { ok: false, cancelled: true } }
    })
    const oldWaiter = h.lane.retryMetadata()
    const old = h.currentOperation()!
    const oldState = h.state.value
    await vi.advanceTimersByTimeAsync(120_000)
    await expect(oldWaiter).resolves.toBe(false)
    // A maliciously late ready publication retains the old attempt number.
    let completed = false
    const retry = h.lane.retryMetadata({ explicit: true }).then(ok => { completed = true; return ok })
    old.finish({ ok: true })
    h.state.value = { ...oldState, phase: 'ready', outcome: { ok: true } }
    await Promise.resolve()
    expect(completed).toBe(false)
    expect(h.lane.phase.value).toBe('loading')
    h.finish({ ok: true })
    await expect(retry).resolves.toBe(true)
  })

  it.each(['key', 'authority'] as const)('fences a renewed recovery on %s replacement', async scope => {
    const h = harness()
    h.begin()
    await vi.advanceTimersByTimeAsync(120_000)
    const retry = h.lane.retryMetadata({ explicit: true })
    const next = scope === 'key' ? h.sessionKey : h.authority
    next.value = 'other'
    await expect(retry).resolves.toBe(false)
    expect(h.lane.explicitRetryAvailable.value).toBe(false)
    expect(h.lane.phase.value).toBe('idle')
  })

  it('preserves a server not-before time when explicit retry renews an expired budget', async () => {
    const h = harness()
    h.begin().finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true, 180_000) })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(h.lane.terminal.value).toBe(true)
    const retried = h.lane.retryMetadata({ explicit: true })
    await vi.advanceTimersByTimeAsync(59_999)
    expect(h.runAttempt).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(h.runAttempt).toHaveBeenCalledOnce()
    h.finish({ ok: true })
    await expect(retried).resolves.toBe(true)
  })

  it('rejects old waiters and gated work on subsequent authority loss or replacement', async () => {
    const h = harness()
    h.begin().finish({ ok: false, error: new SessionReadFailure('busy', 'Busy', true, 60_000) })
    const waiting = h.lane.retryMetadata()
    const gate = expect(h.lane.beforeMetadataRead(new AbortController().signal)).rejects.toMatchObject({ name: 'AbortError' })
    h.authority.value = ''
    h.authority.value = 'owner-b'
    await expect(waiting).resolves.toBe(false)
    await gate
    expect(h.runAttempt).not.toHaveBeenCalled()
  })

  it('fences application and releases waiters on dispose', async () => {
    const h = harness()
    const waiting = h.lane.retryMetadata()
    h.lane.dispose()
    await expect(waiting).resolves.toBe(false)
    expect(h.lane.canApplyMetadata()).toBe(false)
    await vi.advanceTimersByTimeAsync(180_000)
    expect(h.runAttempt).toHaveBeenCalledOnce()
  })
})
