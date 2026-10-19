import { readonly, ref, shallowRef, watch, type Ref } from 'vue'
import { SessionReadFailure, type SessionReadLease } from '@/modules/sessionReadLifecycle'
import { SESSION_PHASE_ATTEMPT_BUDGET_MS, SESSION_RECOVERY_BUDGET_MS,
  retryAfterMs } from './sessionBootstrapContract'
import type { SessionMetadataOutcome, SessionMetadataRetryOptions,
  SessionMetadataState } from './useChatSessionSubscription'

interface MetadataIncident {
  expiresAt: number
  notBefore: number
  attempts: number
  floor: number
  requested: boolean
  failure: unknown
  terminal: SessionMetadataOutcome | null
  terminalReason: 'budget-exhausted' | 'rejected' | null
}

interface MetadataRecoveryOptions {
  sessionKey: Ref<string>
  authority: () => string
  state: Readonly<Ref<SessionMetadataState>>
  connectionState?: Readonly<Ref<string>>
  livePhase: Readonly<Ref<string>>
  currentLease: () => SessionReadLease | null
  canRetry: () => boolean
  runAttempt: (options: SessionMetadataRetryOptions) => Promise<SessionMetadataOutcome>
  cancelAttempt: () => void
}

export interface MetadataRecoveryRequest extends SessionMetadataRetryOptions {
  /** Only a deliberate recovery action may renew an exhausted incident. */
  explicit?: boolean
}

/** Private lane owned by the session bootstrap, including physical read admission. */
export function createSessionMetadataRecovery(options: MetadataRecoveryOptions) {
  const phase = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const error = shallowRef<unknown>(null)
  const terminal = ref(false)
  const explicitRetryAvailable = ref(false)
  const enabled = ref(false)
  const incidents = new Map<string, MetadataIncident>()
  let key = options.sessionKey.value
  let authority = options.authority()
  let authorityObserved = Boolean(authority)
  let owner = {}
  let disposed = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let deadlineTimer: ReturnType<typeof setTimeout> | null = null
  let running: object | null = null
  let observed: SessionMetadataState | null = null
  let refreshing = false
  let refreshAgain = false
  const waiters = new Set<(ok: boolean) => void>()
  const admissionWaiters = new Set<() => void>()
  const identity = () => `${authority}\0${key}`
  const now = () => performance.now()
  const abortError = () => Object.assign(new Error('Metadata recovery owner changed.'), { name: 'AbortError' })

  function clearRetry() {
    if (retryTimer !== null) clearTimeout(retryTimer)
    retryTimer = null
  }
  function clearDeadline() {
    if (deadlineTimer !== null) clearTimeout(deadlineTimer)
    deadlineTimer = null
  }
  function settle(ok: boolean) {
    for (const finish of [...waiters]) finish(ok)
  }
  function wakeAdmission() {
    for (const wake of [...admissionWaiters]) wake()
  }
  function incident(create = false): MetadataIncident | undefined {
    let current = incidents.get(identity())
    if (!current && create && key) {
      current = { expiresAt: now() + SESSION_RECOVERY_BUDGET_MS, notBefore: 0,
        attempts: 0, floor: options.state.value.attempt, requested: false,
        failure: null, terminal: null, terminalReason: null }
      incidents.set(identity(), current)
      while (incidents.size > 64) incidents.delete(incidents.keys().next().value!)
    }
    return current
  }
  function publishTerminal(current: MetadataIncident) {
    if (incident() !== current || !current.terminal) return
    phase.value = 'error'
    error.value = current.terminal.error
    terminal.value = true
    explicitRetryAvailable.value = current.terminalReason === 'budget-exhausted'
  }
  function stop(current: MetadataIncident, outcome: SessionMetadataOutcome,
    reason: MetadataIncident['terminalReason'] = 'rejected') {
    if (incident() !== current) return
    if (!current.terminal) {
      current.terminal = outcome
      current.terminalReason = reason
    }
    publishTerminal(current)
    clearRetry()
    clearDeadline()
    running = null
    options.cancelAttempt()
    settle(false)
    wakeAdmission()
  }
  function expired(current: MetadataIncident): boolean {
    // An owner can leave and revisit this incident. Restore its visible result
    // without re-cancelling reads or settling unrelated replacement waiters.
    if (current.terminal) {
      publishTerminal(current)
      return true
    }
    if (!current.terminal && now() >= current.expiresAt) {
      stop(current, { ok: false, error: new SessionReadFailure(
        'budget-exhausted', 'Session metadata recovery deadline elapsed.', false) }, 'budget-exhausted')
    }
    return Boolean(current.terminal)
  }
  function armDeadline(current: MetadataIncident) {
    clearDeadline()
    if (current.terminal) return
    deadlineTimer = setTimeout(() => {
      deadlineTimer = null
      if (incident() === current) expired(current)
    }, Math.max(0, current.expiresAt - now()))
  }
  function syncOwner() {
    const nextKey = options.sessionKey.value
    const nextAuthority = options.authority()
    if (nextKey === key && nextAuthority === authority) return
    const firstBinding = nextKey === key && !authorityObserved && !authority && Boolean(nextAuthority)
    const previous = incident()
    if (firstBinding && previous) {
      incidents.delete(identity())
      incidents.set(`${nextAuthority}\0${nextKey}`, previous)
    }
    key = nextKey
    authority = nextAuthority
    authorityObserved ||= Boolean(nextAuthority)
    if (firstBinding) return
    owner = {}
    running = null
    observed = options.state.value
    clearRetry()
    clearDeadline()
    settle(false)
    wakeAdmission()
    options.cancelAttempt()
    phase.value = 'idle'
    error.value = null
    terminal.value = false
    explicitRetryAvailable.value = false
  }
  function canApplyMetadata(): boolean {
    syncOwner()
    const current = incident()
    return !disposed && enabled.value && Boolean(key) && (!current || !expired(current))
  }
  function publishFailure(current: MetadataIncident, outcome: SessionMetadataOutcome) {
    if (outcome.cancelled) { current.requested = true; return }
    // Metadata has a typed operation contract. Never apply the legacy live
    // recovery message classifier to a terminal contract/domain failure.
    if (!(outcome.error instanceof SessionReadFailure && outcome.error.retryable
      && outcome.error.kind !== 'aborted') || outcome.sessionMissing) {
      stop(current, outcome)
      return
    }
    current.failure = outcome.error
    current.requested = true
    current.notBefore = Math.max(current.notBefore, now() + Math.max(
      retryAfterMs(outcome.error), Math.min(15_000, 500 * 2 ** Math.min(current.attempts++, 5)),
    ))
    error.value = outcome.error
    wakeAdmission()
  }
  function pump(current: MetadataIncident) {
    if (disposed || !enabled.value || running || expired(current)) return
    if (!current.requested || !options.canRetry()
      || options.connectionState && options.connectionState.value !== 'connected') return
    const state = options.state.value
    if (state.phase === 'loading' && state.lease === options.currentLease()) return
    clearRetry()
    const delay = current.notBefore - now()
    if (delay > 0) {
      retryTimer = setTimeout(() => { retryTimer = null; refresh() }, Math.min(delay, current.expiresAt - now()))
      return
    }
    const token = {}
    const capturedOwner = owner
    running = token
    current.requested = false
    phase.value = 'loading'
    void options.runAttempt({ timeoutMs: Math.max(1, Math.min(
      SESSION_PHASE_ATTEMPT_BUDGET_MS, current.expiresAt - now(),
    )) }).then(outcome => {
      // Normal adapters publish the typed state before resolving. This also
      // handles an adapter that returns early without beginning an attempt.
      if (owner !== capturedOwner || running !== token || incident() !== current || expired(current)) return
      if (!outcome.ok && options.state.value.phase !== 'error') publishFailure(current, outcome)
    }, cause => {
      if (owner === capturedOwner && running === token && incident() === current) {
        publishFailure(current, { ok: false, error: cause instanceof SessionReadFailure ? cause
          : new SessionReadFailure('unavailable', 'Session metadata recovery failed.', false) })
      }
    }).finally(() => {
      if (running === token) { running = null; refresh() }
    })
  }
  function refresh() {
    if (refreshing) { refreshAgain = true; return }
    refreshing = true
    try {
      do {
        refreshAgain = false
        syncOwner()
        if (disposed || !enabled.value) continue
        const state = options.state.value
        const stateCurrent = state.key === key && state.lease !== null && state.lease === options.currentLease()
        const changed = state !== observed
        observed = state
        let current = incident()
        if (stateCurrent && state.phase === 'loading' && !current) {
          current = incident(true)!
          current.floor = state.attempt - 1
        }
        if (!current) continue
        if (expired(current)) {
          if (stateCurrent && state.phase === 'loading') options.cancelAttempt()
          continue
        }
        armDeadline(current)
        phase.value = 'loading'
        terminal.value = false
        explicitRetryAvailable.value = false
        if (stateCurrent && changed && state.attempt > current.floor) {
          if (state.phase === 'ready') {
            incidents.delete(identity())
            clearRetry()
            clearDeadline()
            phase.value = 'ready'
            error.value = null
            settle(true)
            wakeAdmission()
            continue
          }
          if (state.phase === 'error' && state.outcome) publishFailure(current, state.outcome)
        }
        pump(current)
      } while (refreshAgain)
    } finally { refreshing = false }
  }

  // All physical hydration calls, including those created by a replacement
  // lease, pass this admission gate. Their lease signal owns physical lifetime.
  async function beforeMetadataRead(signal: AbortSignal): Promise<void> {
    syncOwner()
    const capturedOwner = owner
    if (disposed || !enabled.value || !key || signal.aborted) throw abortError()
    const current = incident(true)!
    phase.value = 'loading'
    armDeadline(current)
    for (;;) {
      if (disposed || !enabled.value || owner !== capturedOwner || incident() !== current || signal.aborted) throw abortError()
      if (expired(current)) throw current.terminal!.error
      const delay = current.notBefore - now()
      if (delay <= 0) return
      await new Promise<void>(resolve => {
        const finish = () => {
          clearTimeout(timer)
          signal.removeEventListener('abort', finish)
          admissionWaiters.delete(finish)
          resolve()
        }
        const timer = setTimeout(finish, Math.min(delay, current.expiresAt - now()))
        signal.addEventListener('abort', finish, { once: true })
        admissionWaiters.add(finish)
        if (signal.aborted) finish()
      })
    }
  }
  function retryMetadata(request: MetadataRecoveryRequest = {}): Promise<boolean> {
    syncOwner()
    if (disposed || !enabled.value || !key || request.signal?.aborted) return Promise.resolve(false)
    let current = incident(true)!
    // Concurrent calls join the same incident; shorter waiters cannot renew
    // or cancel the shared operation. Explicit renewal keeps server hints.
    expired(current)
    if (request.explicit && current.terminalReason === 'budget-exhausted') {
      // Replace the episode so a gate or completion belonging to the exhausted
      // episode cannot be revived by the user's new admission.
      current = { expiresAt: now() + SESSION_RECOVERY_BUDGET_MS,
        notBefore: current.notBefore, attempts: 0, floor: options.state.value.attempt,
        requested: false, failure: null, terminal: null, terminalReason: null }
      incidents.set(identity(), current)
      terminal.value = false
      explicitRetryAvailable.value = false
      error.value = null
    }
    if (expired(current)) return Promise.resolve(false)
    current.requested = true
    const result = new Promise<boolean>(resolve => {
      let timer: ReturnType<typeof setTimeout> | undefined
      const finish = (ok: boolean) => {
        if (!waiters.delete(finish)) return
        clearTimeout(timer)
        request.signal?.removeEventListener('abort', abort)
        resolve(ok)
      }
      const abort = () => finish(false)
      waiters.add(finish)
      request.signal?.addEventListener('abort', abort, { once: true })
      if (request.timeoutMs !== undefined) timer = setTimeout(abort, Math.max(1, request.timeoutMs))
    })
    refresh()
    return result
  }
  const stopWatch = watch([options.sessionKey, options.authority, options.state,
    options.livePhase, () => options.connectionState?.value, enabled], refresh, { flush: 'sync' })

  function pause(rejectWaiters = true) {
    enabled.value = false
    running = null
    clearRetry()
    options.cancelAttempt()
    wakeAdmission()
    if (rejectWaiters) { clearDeadline(); settle(false); phase.value = 'idle' }
  }
  return {
    phase: readonly(phase), error: readonly(error), terminal: readonly(terminal),
    explicitRetryAvailable: readonly(explicitRetryAvailable),
    retryMetadata, beforeMetadataRead, canApplyMetadata,
    activate() { enabled.value = true; refresh() },
    pause,
    dispose() {
      disposed = true
      owner = {}
      stopWatch()
      pause()
      incidents.clear()
    },
  }
}
