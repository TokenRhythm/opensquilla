import { ref, type Ref } from 'vue'

import { RpcTimeoutError } from '@/lib/rpc'
import type { SessionSubscriptionOutcome } from '@/composables/chat/useChatSessionSubscription'
import {
  SESSION_BOOTSTRAP_BUDGET_MS,
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  isRpcTimeout,
  retryAfterMs,
  shouldRetrySessionPhase,
  type SessionBootstrapPhaseContext,
  type SessionHistoryPhase,
  type SessionLivePhase,
  type SessionPhaseResult,
} from '@/composables/chat/sessionBootstrapContract'

interface PhaseRuntime<T> {
  attempts: number
  deadlineAt: number
  running: boolean
  promise: Promise<T>
  result: T | null
  skipSnapshot: boolean
}

interface ActiveBootstrap {
  generation: number
  key: string
  includeHistory: boolean
  controller: AbortController
  liveQueueSequence: number
  liveQueueWaiters: Set<{
    minimum: number
    resolve: (ready: boolean) => void
  }>
  freshLiveOutageForHistoryRetry: boolean
  history: PhaseRuntime<SessionPhaseResult>
  live: PhaseRuntime<SessionSubscriptionOutcome>
}

export interface SessionBootstrapRun {
  generation: number
  history: Promise<SessionPhaseResult>
  live: Promise<SessionSubscriptionOutcome>
}

export interface UseChatSessionBootstrapOptions {
  sessionKey: Ref<string>
  loadHistory: (
    context: SessionBootstrapPhaseContext,
    retry: boolean,
  ) => Promise<SessionPhaseResult | void>
  subscribeSession: (
    context: SessionBootstrapPhaseContext,
  ) => Promise<SessionSubscriptionOutcome>
  cancelHistory: () => void
  cancelSubscription: () => void
  unsubscribeSession: (key?: string) => void | Promise<void>
}

const EMPTY_HISTORY_RESULT: SessionPhaseResult = { ok: true }
const UNAVAILABLE_LIVE_RESULT: SessionSubscriptionOutcome = {
  authoritative: false,
  live: false,
  backgroundOnly: false,
}

function historyRuntime(deadlineAt: number): PhaseRuntime<SessionPhaseResult> {
  return {
    attempts: 0,
    deadlineAt,
    running: false,
    promise: Promise.resolve(EMPTY_HISTORY_RESULT),
    result: null,
    skipSnapshot: false,
  }
}

function liveRuntime(deadlineAt: number): PhaseRuntime<SessionSubscriptionOutcome> {
  return {
    attempts: 0,
    deadlineAt,
    running: false,
    promise: Promise.resolve(UNAVAILABLE_LIVE_RESULT),
    result: null,
    skipSnapshot: false,
  }
}

export function useChatSessionBootstrap(options: UseChatSessionBootstrapOptions) {
  const historyPhase = ref<SessionHistoryPhase>('idle')
  const livePhase = ref<SessionLivePhase>('idle')
  let generation = 0
  let active: ActiveBootstrap | null = null
  // A successful live phase arms exactly one new automatic recovery budget
  // for the next external disconnect. A terminal recovery stays terminal
  // across the RpcClient's background reconnect cycles until the user retries.
  let connectionRecoveryArmed = false

  function isCurrent(run: ActiveBootstrap): boolean {
    return (
      active === run
      && generation === run.generation
      && options.sessionKey.value === run.key
      && !run.controller.signal.aborted
    )
  }

  function contextFor(
    run: ActiveBootstrap,
    phase: PhaseRuntime<unknown>,
    attempt: 0 | 1,
  ): SessionBootstrapPhaseContext {
    const now = Date.now()
    return {
      generation: run.generation,
      key: run.key,
      attempt,
      deadlineAt: phase.deadlineAt,
      attemptDeadlineAt: Math.min(
        phase.deadlineAt,
        now + SESSION_PHASE_ATTEMPT_BUDGET_MS,
      ),
      signal: run.controller.signal,
      skipSnapshot: phase.skipSnapshot,
      ...(phase === run.live
        ? {
            markLiveSubscribeSent: () => markLiveSubscribeSent(run),
            waitForHistoryTerminal: () => run.history.promise,
          }
        : {}),
    }
  }

  function markLiveSubscribeSent(run: ActiveBootstrap) {
    if (!isCurrent(run)) return
    run.liveQueueSequence += 1
    for (const waiter of [...run.liveQueueWaiters]) {
      if (run.liveQueueSequence < waiter.minimum) continue
      run.liveQueueWaiters.delete(waiter)
      waiter.resolve(true)
    }
  }

  async function waitForLiveSubscribeSent(
    run: ActiveBootstrap,
    minimum: number,
    deadlineAt: number,
  ): Promise<boolean> {
    if (!isCurrent(run)) return false
    if (run.liveQueueSequence >= minimum) return true
    const remaining = deadlineAt - Date.now()
    if (remaining <= 0) return false
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
        run.controller.signal.removeEventListener('abort', onAbort)
        run.liveQueueWaiters.delete(waiter)
        resolve(ready)
      }
      const onAbort = () => finish(false)
      const timer = setTimeout(() => finish(false), remaining)
      run.liveQueueWaiters.add(waiter)
      run.controller.signal.addEventListener('abort', onAbort, { once: true })
      if (run.liveQueueSequence >= minimum) finish(true)
    })
  }

  function requiresFreshLiveQueue(error: unknown): boolean {
    if (isRpcTimeout(error)) return true
    const message = error instanceof Error ? error.message.toLowerCase() : ''
    return (
      message.includes('connection')
      || message.includes('socket')
      || message.includes('not connected')
      || message.includes('network')
    )
  }

  async function waitBeforeRetry(
    error: unknown,
    run: ActiveBootstrap,
    deadlineAt: number,
  ): Promise<boolean> {
    const remaining = deadlineAt - Date.now()
    if (remaining <= 0 || !isCurrent(run)) return false
    const delayMs = Math.min(retryAfterMs(error), remaining)
    if (delayMs <= 0) return true
    return new Promise(resolve => {
      let settled = false
      const finish = (ready: boolean) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        run.controller.signal.removeEventListener('abort', onAbort)
        resolve(ready)
      }
      const onAbort = () => finish(false)
      const timer = setTimeout(() => finish(isCurrent(run)), delayMs)
      run.controller.signal.addEventListener('abort', onAbort, { once: true })
    })
  }

  function normalizeHistoryResult(
    result: SessionPhaseResult | void,
  ): SessionPhaseResult {
    return result ?? EMPTY_HISTORY_RESULT
  }

  function runHistoryPhase(
    run: ActiveBootstrap,
    retryFirst: boolean,
  ): Promise<SessionPhaseResult> {
    const phase = run.history
    if (phase.running) return phase.promise
    phase.running = true
    phase.result = null
    if (isCurrent(run)) historyPhase.value = 'loading'

    phase.promise = (async () => {
      let lastResult: SessionPhaseResult = {
        ok: false,
        error: new RpcTimeoutError('chat.history', 0),
      }
      let requiredLiveQueueSequence = Math.max(1, run.liveQueueSequence)
      while (phase.attempts < 2 && isCurrent(run)) {
        if (Date.now() >= phase.deadlineAt) break
        if (!await waitForLiveSubscribeSent(
          run,
          requiredLiveQueueSequence,
          phase.deadlineAt,
        )) {
          break
        }
        const liveQueueSequenceForAttempt = run.liveQueueSequence
        const attempt = phase.attempts as 0 | 1
        phase.attempts += 1
        const context = contextFor(run, phase, attempt)
        try {
          lastResult = normalizeHistoryResult(
            await options.loadHistory(context, retryFirst || attempt > 0),
          )
        } catch (error: unknown) {
          lastResult = {
            ok: false,
            error,
            cancelled: isRpcAbort(error) || run.controller.signal.aborted,
          }
        }
        if (!isCurrent(run) || lastResult.cancelled) {
          return { ...lastResult, ok: false, cancelled: true }
        }
        if (lastResult.ok) {
          historyPhase.value = 'ready'
          return lastResult
        }
        if (requiresFreshLiveQueue(lastResult.error)) {
          requiredLiveQueueSequence = liveQueueSequenceForAttempt + 1
        }
        if (
          phase.attempts >= 2
          || !shouldRetrySessionPhase(lastResult.error)
          || !await waitBeforeRetry(lastResult.error, run, phase.deadlineAt)
        ) {
          break
        }
      }
      if (isCurrent(run)) historyPhase.value = 'error'
      return lastResult
    })().finally(() => {
      phase.running = false
      phase.result = null
    })
    return phase.promise
  }

  function runLivePhase(run: ActiveBootstrap): Promise<SessionSubscriptionOutcome> {
    const phase = run.live
    if (phase.running) return phase.promise
    phase.running = true
    phase.result = null
    if (isCurrent(run)) livePhase.value = 'connecting'

    phase.promise = (async () => {
      let lastResult: SessionSubscriptionOutcome = {
        ...UNAVAILABLE_LIVE_RESULT,
        error: new RpcTimeoutError('sessions.messages.subscribe', 0),
      }
      while (phase.attempts < 2 && isCurrent(run)) {
        if (Date.now() >= phase.deadlineAt) break
        const attempt = phase.attempts as 0 | 1
        phase.attempts += 1
        const context = contextFor(run, phase, attempt)
        try {
          lastResult = await options.subscribeSession(context)
        } catch (error: unknown) {
          lastResult = {
            ...UNAVAILABLE_LIVE_RESULT,
            error,
            cancelled: isRpcAbort(error) || run.controller.signal.aborted,
          }
        }
        if (!isCurrent(run) || lastResult.cancelled) {
          return { ...lastResult, authoritative: false, cancelled: true }
        }
        if (lastResult.skipSnapshotOnRetry) phase.skipSnapshot = true
        if (lastResult.authoritative) {
          livePhase.value = 'ready'
          connectionRecoveryArmed = true
          return lastResult
        }
        if (
          phase.attempts >= 2
          || !shouldRetrySessionPhase(lastResult.error)
          || !await waitBeforeRetry(lastResult.error, run, phase.deadlineAt)
        ) {
          break
        }
      }
      if (isCurrent(run)) livePhase.value = 'degraded'
      return lastResult
    })().finally(() => {
      phase.running = false
    })
    return phase.promise
  }

  function createRun(key: string, includeHistory: boolean): ActiveBootstrap {
    const deadlineAt = Date.now() + SESSION_BOOTSTRAP_BUDGET_MS
    const run: ActiveBootstrap = {
      generation: ++generation,
      key,
      includeHistory,
      controller: new AbortController(),
      liveQueueSequence: 0,
      liveQueueWaiters: new Set(),
      freshLiveOutageForHistoryRetry: false,
      history: historyRuntime(deadlineAt),
      live: liveRuntime(deadlineAt),
    }
    active?.controller.abort()
    active = run
    connectionRecoveryArmed = false
    return run
  }

  function publicRun(run: ActiveBootstrap): SessionBootstrapRun {
    return {
      generation: run.generation,
      history: run.history.promise,
      live: run.live.promise,
    }
  }

  function startSessionBootstrap(optionsForStart: {
    includeHistory?: boolean
    force?: boolean
  } = {}): SessionBootstrapRun {
    const key = options.sessionKey.value
    const includeHistory = optionsForStart.includeHistory !== false
    if (!key) {
      return {
        generation,
        history: Promise.resolve(EMPTY_HISTORY_RESULT),
        live: Promise.resolve(UNAVAILABLE_LIVE_RESULT),
      }
    }
    if (
      !optionsForStart.force
      && active
      && active.key === key
      && !active.controller.signal.aborted
    ) {
      if (includeHistory && !active.includeHistory) {
        if (Date.now() >= active.history.deadlineAt) {
          return startSessionBootstrap({ includeHistory: true, force: true })
        }
        active.includeHistory = true
        active.history = historyRuntime(active.live.deadlineAt)
        active.history.promise = runHistoryPhase(active, false)
      }
      return publicRun(active)
    }

    const run = createRun(key, includeHistory)
    // Start live registration immediately. Canonical history is an orthogonal
    // terminal phase, but its first RPC is held behind the fast subscribe ACK
    // so a slow read cannot head-of-line block replay/live delivery.
    run.live.promise = runLivePhase(run)
    run.history.promise = includeHistory
      ? runHistoryPhase(run, false)
      : Promise.resolve(EMPTY_HISTORY_RESULT)
    if (!includeHistory) historyPhase.value = 'ready'
    return publicRun(run)
  }

  function resetHistoryPhaseForManualRetry(run: ActiveBootstrap) {
    run.history = historyRuntime(Date.now() + SESSION_BOOTSTRAP_BUDGET_MS)
  }

  function resetLivePhaseForManualRetry(run: ActiveBootstrap) {
    run.live = liveRuntime(Date.now() + SESSION_BOOTSTRAP_BUDGET_MS)
  }

  function retryHistory(): Promise<SessionPhaseResult> {
    const key = options.sessionKey.value
    const run = active
    if (!run || run.key !== key || run.controller.signal.aborted) {
      return startSessionBootstrap({ includeHistory: true, force: true }).history
    }
    if (run.history.running) return run.history.promise
    resetHistoryPhaseForManualRetry(run)
    // A user-initiated history retry is a new recovery operation. If its local
    // timeout recycles an otherwise-authoritative live socket, re-register live
    // with a fresh outage budget instead of inheriting exhausted attempts from
    // the original bootstrap.
    run.freshLiveOutageForHistoryRetry = true
    run.history.promise = runHistoryPhase(run, true)
    return run.history.promise
  }

  function retryLive(): Promise<SessionSubscriptionOutcome> {
    const key = options.sessionKey.value
    const run = active
    if (!run || run.key !== key || run.controller.signal.aborted) {
      return startSessionBootstrap({ includeHistory: false, force: true }).live
    }
    if (run.live.running) return run.live.promise
    resetLivePhaseForManualRetry(run)
    run.live.promise = runLivePhase(run)
    return run.live.promise
  }

  function cancelSessionBootstrap(unsubscribe = true) {
    const cancelled = active
    ++generation
    active = null
    connectionRecoveryArmed = false
    cancelled?.controller.abort()
    if (cancelled) {
      for (const waiter of cancelled.liveQueueWaiters) waiter.resolve(false)
      cancelled.liveQueueWaiters.clear()
    }
    options.cancelHistory()
    options.cancelSubscription()
    historyPhase.value = 'idle'
    livePhase.value = 'idle'
    if (unsubscribe && cancelled?.key) {
      void options.unsubscribeSession(cancelled.key)
    }
  }

  function isSessionBootstrapCurrent(
    candidateGeneration: number,
    key = options.sessionKey.value,
  ): boolean {
    return Boolean(
      active
      && active.generation === candidateGeneration
      && active.key === key
      && isCurrent(active),
    )
  }

  function handleConnectionState(
    state: string,
    includeHistory = true,
  ): SessionBootstrapRun | undefined {
    if (state === 'disconnected') {
      const key = options.sessionKey.value
      if (!key) return
      const run = active
      const currentRun = run
        && run.key === key
        && !run.controller.signal.aborted
      if (currentRun && (run.history.running || run.live.running)) {
        const liveWasReady = livePhase.value === 'ready'
        if (run.live.running || liveWasReady) {
          livePhase.value = 'connecting'
        }
        // A timeout/abort owned by this run may recycle the socket. Keep the
        // original absolute deadline. If live had already succeeded while
        // history was still running, recover live within that same budget.
        // A terminal degraded live phase stays terminal: a sibling history
        // timeout must not silently grant it attempts three and four.
        if (!run.live.running && liveWasReady) {
          const priorLive = run.live
          const freshOutage = run.freshLiveOutageForHistoryRetry
          run.freshLiveOutageForHistoryRetry = false
          run.live = {
            ...liveRuntime(run.history.deadlineAt),
            attempts: freshOutage ? 0 : priorLive.attempts,
            skipSnapshot: priorLive.skipSnapshot,
          }
          run.live.promise = runLivePhase(run)
        }
        if (run.live.running || liveWasReady) connectionRecoveryArmed = false
        return publicRun(run)
      }
      // Once a recovery budget reaches a terminal degraded state, background
      // reconnect churn must not turn the honest terminal state back into an
      // endless "connecting" indicator. Only an authoritative live phase can
      // arm a fresh outage budget.
      if (!connectionRecoveryArmed) return currentRun ? publicRun(run) : undefined
      // This is a new outage after an authoritative connection. Start its
      // wall-clock budget immediately; do not wait indefinitely for _state
      // "connected" before the coordinator begins counting.
      return startSessionBootstrap({ includeHistory, force: true })
    }
    if (state !== 'connected' || !options.sessionKey.value) return

    const run = active
    if (!run || run.key !== options.sessionKey.value || run.controller.signal.aborted) {
      return startSessionBootstrap({ includeHistory, force: true })
    }
    // The phase waiting for this connection owns its retry budget. A terminal
    // run must not be silently given a third attempt by a late reconnect.
    return publicRun(run)
  }

  return {
    historyPhase,
    livePhase,
    startSessionBootstrap,
    cancelSessionBootstrap,
    retryHistory,
    retryLive,
    handleConnectionState,
    isSessionBootstrapCurrent,
  }
}
