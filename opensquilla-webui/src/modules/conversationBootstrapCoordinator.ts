/**
 * Transport-neutral ownership for a conversation bootstrap run.
 *
 * A bootstrap combines two asynchronous phases (history and live) and can be
 * interrupted by a route handoff or by a physical connection flap.  The
 * phase implementation still lives in the legacy composable for now, but the
 * identity rules belong here so another transport or application adapter
 * cannot accidentally reimplement them:
 *
 *   one active key -> monotonic generation -> abort the predecessor
 *   handoff epoch -> deferred connection transitions -> explicit replay
 *
 * This module intentionally has no Vue, RpcClient, generated wire, or RPC
 * method imports.  It is an ownership seam, not a transport wrapper.
 */

export interface ConversationBootstrapRunToken {
  readonly generation: number
  readonly key: string
  /** The deadline is absolute so retries cannot extend one bootstrap budget. */
  readonly deadlineAt: number
  /** Include-history may be upgraded by a caller reusing the same run. */
  includeHistory: boolean
  readonly controller: AbortController
}

/** Mutable phase bookkeeping shared by history/live execution adapters. */
export interface ConversationBootstrapPhase<T> {
  attempts: number
  readonly deadlineAt: number
  running: boolean
  promise: Promise<T>
  result: T | null
  skipSnapshot: boolean
}

export interface ConversationBootstrapCriticalQueue {
  /** Resolves after the critical frames are queued on a compatible socket. */
  readonly promise: Promise<void>
  readonly historyRequired: boolean
  readonly liveSocketGeneration: number | null
  readonly historySocketGeneration: number | null
  readonly liveQueueSequence: number
  readonly released: boolean
  markLiveSubscribeSent(socketGeneration: number): void
  markHistoryRequestSent(socketGeneration: number): void
  markLiveTerminal(): void
  markHistoryTerminal(): void
  /** Resolve this queue and every waiter when the owning run is cancelled. */
  cancel(): void
  waitForLiveSubscribeSent(
    minimum: number,
    deadlineAt: number,
    signal: AbortSignal,
    isCurrent: () => boolean,
  ): Promise<boolean>
  /** Used internally to bridge an older queue to its replacement epoch. */
  release(): void
}

interface CriticalQueueSequenceLedger {
  sequence: number
  waiters: Set<{
    minimum: number
    resolve: (ready: boolean) => void
  }>
}

const queueSequenceLedgers = new WeakMap<object, CriticalQueueSequenceLedger>()

export interface ConversationBootstrapRetryWaitOptions {
  delayMs: number
  deadlineAt: number
  signal: AbortSignal
  isCurrent: () => boolean
}

/**
 * Wait for a bounded retry without coupling the timer to an RPC client.
 * `isCurrent` is supplied by the owner because a route key can change before
 * the transport emits its cancellation event.
 */
export function waitForConversationBootstrapRetry(
  options: ConversationBootstrapRetryWaitOptions,
): Promise<boolean> {
  const remaining = options.deadlineAt - Date.now()
  if (remaining <= 0 || !options.isCurrent()) return Promise.resolve(false)
  // Keep the caller's retry policy intact while bounding it by the absolute
  // bootstrap deadline (the legacy wrapper used the same min operation).
  const delayMs = Math.min(Math.max(0, options.delayMs), remaining)
  if (delayMs <= 0) return Promise.resolve(true)
  return new Promise(resolve => {
    let settled = false
    const finish = (ready: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      options.signal.removeEventListener('abort', onAbort)
      resolve(ready)
    }
    const onAbort = () => finish(false)
    const timer = setTimeout(
      () => finish(options.isCurrent()),
      delayMs,
    )
    options.signal.addEventListener('abort', onAbort, { once: true })
    if (options.signal.aborted) finish(false)
  })
}

/**
 * Build the critical-frame barrier shared by history and live phases.
 * A queue releases only when live (and, when required, history) has been
 * placed on the same socket, or when the phase has terminated without being
 * able to queue.  This keeps optional reads from blocking live delivery.
 */
export function createConversationBootstrapCriticalQueue(
  historyRequired: boolean,
  liveSocketGeneration: number | null = null,
  initialLiveQueueSequence = 0,
  sequenceLedger?: CriticalQueueSequenceLedger,
): ConversationBootstrapCriticalQueue {
  let resolvePromise: () => void = () => {}
  const promise = new Promise<void>(resolve => {
    resolvePromise = resolve
  })
  let released = false
  let historySocketGeneration: number | null = null
  let liveTerminal = false
  let historyTerminal = !historyRequired
  const ledger = sequenceLedger ?? {
    sequence: Math.max(0, initialLiveQueueSequence),
    waiters: new Set<{
      minimum: number
      resolve: (ready: boolean) => void
    }>(),
  }

  function releaseIfReady() {
    if (released) return
    const liveQueued = liveSocketGeneration !== null
    const historyQueued = (
      !historyRequired
      || historySocketGeneration !== null
    )
    const queuedOnSameSocket = (
      liveQueued
      && historyQueued
      && (
        !historyRequired
        || liveSocketGeneration === historySocketGeneration
      )
    )
    const terminalWithoutQueue = (
      (liveTerminal || (historyRequired && historyTerminal))
      && (liveQueued || liveTerminal)
      && (
        !historyRequired
        || historyQueued
        || historyTerminal
      )
    )
    if (!queuedOnSameSocket && !terminalWithoutQueue) return
    released = true
    resolvePromise()
  }

  function release() {
    if (released) return
    released = true
    resolvePromise()
  }

  function cancel() {
    release()
    for (const waiter of ledger.waiters) waiter.resolve(false)
    ledger.waiters.clear()
  }

  const queue: ConversationBootstrapCriticalQueue = {
    promise,
    get historyRequired() {
      return historyRequired
    },
    get liveSocketGeneration() {
      return liveSocketGeneration
    },
    get historySocketGeneration() {
      return historySocketGeneration
    },
    get liveQueueSequence() {
      return ledger.sequence
    },
    get released() {
      return released
    },
    markLiveSubscribeSent(socketGeneration) {
      liveSocketGeneration = socketGeneration
      ledger.sequence += 1
      for (const waiter of [...ledger.waiters]) {
        if (ledger.sequence < waiter.minimum) continue
        ledger.waiters.delete(waiter)
        waiter.resolve(true)
      }
      releaseIfReady()
    },
    markHistoryRequestSent(socketGeneration) {
      historySocketGeneration = socketGeneration
      releaseIfReady()
    },
    markLiveTerminal() {
      liveTerminal = true
      releaseIfReady()
    },
    markHistoryTerminal() {
      historyTerminal = true
      releaseIfReady()
    },
    cancel,
    release,
    waitForLiveSubscribeSent(minimum, deadlineAt, signal, isCurrent) {
      if (!isCurrent() || signal.aborted) return Promise.resolve(false)
      if (ledger.sequence >= minimum) return Promise.resolve(true)
      const remaining = deadlineAt - Date.now()
      if (remaining <= 0) return Promise.resolve(false)
      return new Promise(resolve => {
        let settled = false
        const waiter = {
          minimum,
          resolve: (ready: boolean) => finish(ready),
        }
        const finish = (ready: boolean) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          signal.removeEventListener('abort', onAbort)
          ledger.waiters.delete(waiter)
          resolve(ready)
        }
        const onAbort = () => finish(false)
        // A timeout means that this queue epoch never observed the required
        // live frame.  It must stay a negative result even when ownership is
        // still current; callers use that distinction to stop the phase
        // rather than treating an unqueued request as ready.
        const timer = setTimeout(() => finish(false), remaining)
        ledger.waiters.add(waiter)
        signal.addEventListener('abort', onAbort, { once: true })
        if (signal.aborted || !isCurrent()) finish(false)
        else if (ledger.sequence >= minimum) finish(true)
      })
    },
  }
  queueSequenceLedgers.set(queue, ledger)
  return queue
}

/**
 * Start a replacement socket epoch while preserving consumers waiting on the
 * predecessor promise.  Once the replacement barrier opens, the predecessor
 * opens as well; cancellation of the replacement unwinds both promises.
 */
export function rearmConversationBootstrapCriticalQueue(
  previous: ConversationBootstrapCriticalQueue,
  historyRequired: boolean,
  liveSocketGeneration: number | null = null,
): ConversationBootstrapCriticalQueue {
  const replacement = createConversationBootstrapCriticalQueue(
    historyRequired,
    liveSocketGeneration,
    previous.liveQueueSequence,
    queueSequenceLedgers.get(previous),
  )
  void replacement.promise.then(() => previous.release())
  return replacement
}

/**
 * Initialise a phase without importing a transport or reactive state layer.
 * The execution callback remains in the wrapper until a later slice moves
 * retry policy here; this factory keeps its bookkeeping shape canonical.
 */
export function createConversationBootstrapPhase<T>(
  deadlineAt: number,
  initialResult: T,
): ConversationBootstrapPhase<T> {
  return {
    attempts: 0,
    deadlineAt,
    running: false,
    promise: Promise.resolve(initialResult),
    result: null,
    skipSnapshot: false,
  }
}

export interface ConversationBootstrapConnectionState {
  readonly state: string
  readonly includeHistory: boolean
}

export type ConversationBootstrapHandoffOutcome =
  | 'committed'
  | 'unchanged'
  | 'failed'
  | 'superseded'

export interface ConversationBootstrapHandoffResolution {
  /** False means the caller supplied an older epoch and must do nothing. */
  readonly accepted: boolean
  /** States to replay after a rollback/failed handoff. */
  readonly deferred: readonly ConversationBootstrapConnectionState[]
}

export interface ConversationBootstrapCoordinator<TRun extends ConversationBootstrapRunToken> {
  /** Monotonic identity used for no-key/empty-run responses as well. */
  readonly generation: number
  /** Current run, if one has been started. */
  current(): TRun | null
  /** Start a new run and invalidate the previous one. */
  start<TState extends object>(
    key: string,
    includeHistory: boolean,
    createState: (token: ConversationBootstrapRunToken) => TState,
  ): TRun & TState
  /** Abort and forget the active run; returns it for caller-side cleanup. */
  cancel(): TRun | null
  /** Identity and abort fence shared by every phase callback. */
  isCurrent(run: ConversationBootstrapRunToken, key: string): boolean

  /** Record that navigation owns a newer target until it commits/rolls back. */
  setHandoffTarget(targetKey: string | null, epoch: number): boolean
  /** Resolve a handoff and return only the transitions that should be replayed. */
  resolveHandoff(
    epoch: number,
    outcome: ConversationBootstrapHandoffOutcome,
  ): ConversationBootstrapHandoffResolution
  /** Whether a physical transition must wait for the declared handoff target. */
  shouldDeferConnectionState(currentKey: string): boolean
  /** Coalesce deferred transport states while a handoff is pending. */
  deferConnectionState(state: string, includeHistory: boolean): void

  /** A successful live phase grants one background outage recovery budget. */
  armRecovery(): void
  disarmRecovery(): void
  consumeRecoveryBudget(): boolean
}

export interface ConversationBootstrapCoordinatorOptions {
  /** Keep time injectable so pure tests do not depend on wall-clock timing. */
  now?: () => number
  /** Absolute run budget. A caller may pass zero for a deadline-free test. */
  budgetMs: number
}

/**
 * Build the ownership coordinator.  `createState` lets the legacy wrapper
 * attach its phase/queue state without making this module know those types.
 * The returned object is the token itself, so identity checks remain strict
 * even while the wrapper gradually moves state behind this seam.
 */
export function createConversationBootstrapCoordinator<
  TRun extends ConversationBootstrapRunToken = ConversationBootstrapRunToken,
>(
  options: ConversationBootstrapCoordinatorOptions,
): ConversationBootstrapCoordinator<TRun> {
  const now = options.now ?? (() => Date.now())
  let generation = 0
  let active: TRun | null = null
  let pendingHandoff: { targetKey: string; epoch: number } | null = null
  let deferredConnectionStates: ConversationBootstrapConnectionState[] = []
  let recoveryArmed = false

  function start<TState extends object>(
    key: string,
    includeHistory: boolean,
    createState: (token: ConversationBootstrapRunToken) => TState,
  ): TRun & TState {
    active?.controller.abort()
    const token: ConversationBootstrapRunToken = {
      generation: ++generation,
      key,
      deadlineAt: now() + Math.max(0, options.budgetMs),
      includeHistory,
      controller: new AbortController(),
    }
    const run = Object.assign(token, createState(token)) as TRun & TState
    active = run as unknown as TRun
    recoveryArmed = false
    return run
  }

  function cancel(): TRun | null {
    const cancelled = active
    ++generation
    active = null
    recoveryArmed = false
    cancelled?.controller.abort()
    return cancelled
  }

  function setHandoffTarget(targetKey: string | null, epoch: number): boolean {
    if (!targetKey) return true
    if (pendingHandoff && epoch < pendingHandoff.epoch) return false
    pendingHandoff = { targetKey, epoch }
    return true
  }

  function resolveHandoff(
    epoch: number,
    outcome: ConversationBootstrapHandoffOutcome,
  ): ConversationBootstrapHandoffResolution {
    if (pendingHandoff && epoch < pendingHandoff.epoch) {
      return { accepted: false, deferred: [] }
    }
    pendingHandoff = null
    const deferred = deferredConnectionStates
    deferredConnectionStates = []
    return {
      accepted: true,
      deferred: outcome === 'committed' ? [] : deferred,
    }
  }

  function deferConnectionState(state: string, includeHistory: boolean) {
    if (state === 'disconnected') {
      // A later outage supersedes an already deferred flap. Replaying only
      // this terminal transition is enough to recover the eventual target.
      deferredConnectionStates = [{ state, includeHistory }]
      return
    }
    const previous = deferredConnectionStates[deferredConnectionStates.length - 1]
    if (previous?.state === state) {
      if (includeHistory && !previous.includeHistory) {
        deferredConnectionStates[deferredConnectionStates.length - 1] = {
          state: previous.state,
          includeHistory: true,
        }
      }
      return
    }
    deferredConnectionStates.push({ state, includeHistory })
  }

  return {
    get generation() {
      return generation
    },
    current: () => active,
    start,
    cancel,
    isCurrent(run, key) {
      return active === run
        && generation === run.generation
        && run.key === key
        && !run.controller.signal.aborted
    },
    setHandoffTarget,
    resolveHandoff,
    shouldDeferConnectionState(currentKey) {
      return Boolean(
        pendingHandoff
        && pendingHandoff.targetKey !== currentKey,
      )
    },
    deferConnectionState,
    armRecovery() {
      recoveryArmed = true
    },
    disarmRecovery() {
      recoveryArmed = false
    },
    consumeRecoveryBudget() {
      if (!recoveryArmed) return false
      recoveryArmed = false
      return true
    },
  }
}
