import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'

import type { SessionSubscriptionOutcome } from '@/composables/chat/useChatSessionSubscription'
import {
  SESSION_BOOTSTRAP_BUDGET_MS,
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  retryAfterMs,
  shouldRetrySessionPhase,
  type SessionBootstrapPhaseContext,
  type SessionHistoryPhase,
  type SessionLivePhase,
  type SessionPhaseResult,
} from '@/composables/chat/sessionBootstrapContract'
import {
  createConversationBootstrapPhase,
  createConversationBootstrapCoordinator,
  type ConversationBootstrapPhase,
  type ConversationBootstrapHandoffOutcome,
  type ConversationBootstrapRunToken,
  waitForConversationBootstrapRetry,
} from '@/modules/conversationBootstrapCoordinator'
import type {
  SessionReadLease,
  SessionReadLifecycle,
} from '@/modules/sessionReadLifecycle'

type PhaseRuntime<T> = ConversationBootstrapPhase<T>

interface ActiveBootstrapState {
  readonly lease: SessionReadLease
  history: PhaseRuntime<SessionPhaseResult>
  live: PhaseRuntime<SessionSubscriptionOutcome>
}

type ActiveBootstrap = ConversationBootstrapRunToken & ActiveBootstrapState

export interface SessionBootstrapRun {
  generation: number
  criticalRequestsQueued: Promise<void>
  history: Promise<SessionPhaseResult>
  live: Promise<SessionSubscriptionOutcome>
  /** Physical state was recorded during a logical handoff; no Session work ran. */
  deferred?: boolean
}

export interface UseChatSessionBootstrapOptions {
  sessionKey: Ref<string>
  sessionReadLifecycle: SessionReadLifecycle
  loadHistory: (
    context: SessionBootstrapPhaseContext,
    retry: boolean,
  ) => Promise<SessionPhaseResult | void>
  subscribeSession: (
    context: SessionBootstrapPhaseContext,
  ) => Promise<SessionSubscriptionOutcome>
  /** Production recovery uses the existing lease, preserving subscription authority. */
  reconcileSession?: (context: SessionBootstrapPhaseContext) => Promise<SessionSubscriptionOutcome>
  connectionState?: Readonly<Ref<string>>
  cancelHistory: () => void
  cancelSubscription: () => void
}

const EMPTY_HISTORY_RESULT: SessionPhaseResult = { ok: true }
const UNAVAILABLE_LIVE_RESULT: SessionSubscriptionOutcome = {
  authoritative: false,
  live: false,
  backgroundOnly: false,
}

function historyRuntime(deadlineAt: number): PhaseRuntime<SessionPhaseResult> {
  return createConversationBootstrapPhase(deadlineAt, EMPTY_HISTORY_RESULT)
}

function liveRuntime(deadlineAt: number): PhaseRuntime<SessionSubscriptionOutcome> {
  return createConversationBootstrapPhase(deadlineAt, UNAVAILABLE_LIVE_RESULT)
}

export function useChatSessionBootstrap(options: UseChatSessionBootstrapOptions) {
  const historyPhase = ref<SessionHistoryPhase>('idle')
  const livePhase = ref<SessionLivePhase>('idle')
  let active: ActiveBootstrap | null = null
  let recoveryTimer: ReturnType<typeof setTimeout> | null = null
  let recoveryAttempt = 0
  let queuedReconciliation: { run: ActiveBootstrap, promise: Promise<SessionSubscriptionOutcome> } | null = null
  function clearRecoveryTimer() {
    if (recoveryTimer !== null) clearTimeout(recoveryTimer)
    recoveryTimer = null
  }
  const ownership = createConversationBootstrapCoordinator<ActiveBootstrap>({
    budgetMs: SESSION_BOOTSTRAP_BUDGET_MS,
  })

  function setSessionHandoffTarget(
    targetKey: string | null,
    epoch: number,
    outcome: ConversationBootstrapHandoffOutcome = 'failed',
  ): SessionBootstrapRun | undefined {
    if (targetKey) {
      ownership.setHandoffTarget(targetKey, epoch)
      return
    }
    const resolution = ownership.resolveHandoff(epoch, outcome)
    if (!resolution.accepted) return
    // A committed target starts its own bootstrap before the handoff closes.
    // Replaying older transport transitions would duplicate or preempt that B
    // registration. A rollback keeps A, so replay is required there.
    if (outcome === 'committed') return
    let resumed: SessionBootstrapRun | undefined
    for (const event of resolution.deferred) {
      resumed = handleConnectionState(event.state, event.includeHistory) ?? resumed
    }
    return resumed
  }

  function isCurrent(run: ActiveBootstrap): boolean {
    return ownership.isCurrent(run, options.sessionKey.value)
  }

  function leaseIsCurrent(run: ActiveBootstrap): boolean {
    return options.sessionReadLifecycle.current() === run.lease
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
    }
  }

  function waitBeforeRetry(
    error: unknown,
    run: ActiveBootstrap,
    deadlineAt: number,
  ): Promise<boolean> {
    return waitForConversationBootstrapRetry({
      delayMs: retryAfterMs(error),
      deadlineAt,
      signal: run.controller.signal,
      isCurrent: () => isCurrent(run),
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
    maxAttempts: 1 | 2 = 2,
  ): Promise<SessionPhaseResult> {
    const phase = run.history
    if (phase.running) return phase.promise
    phase.running = true
    phase.result = null
    if (isCurrent(run)) historyPhase.value = 'loading'

    phase.promise = (async () => {
      let lastResult: SessionPhaseResult = {
        ok: false,
        error: new Error('The session history deadline elapsed before the first attempt.'),
      }
      while (phase.attempts < maxAttempts && isCurrent(run)) {
        if (Date.now() >= phase.deadlineAt) break
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
        if (
          phase.attempts >= maxAttempts
          || !shouldRetrySessionPhase(lastResult.error)
          || !await waitBeforeRetry(lastResult.error, run, phase.deadlineAt)
        ) {
          break
        }
      }
      if (isCurrent(run)) historyPhase.value = 'error'
      return lastResult
    })().then(result => {
      phase.result = result
      return result
    }).finally(() => {
      phase.running = false
    })
    return phase.promise
  }

  function runLivePhase(run: ActiveBootstrap, reconcile = false): Promise<SessionSubscriptionOutcome> {
    const phase = run.live
    if (phase.running) return phase.promise
    phase.running = true
    phase.result = null
    if (isCurrent(run)) livePhase.value = 'connecting'

    phase.attempts = 1
    phase.promise = (async () => {
      const context = contextFor(run, phase, 0)
      let result: SessionSubscriptionOutcome
      try {
        result = await (reconcile && options.reconcileSession
          ? options.reconcileSession(context)
          : options.subscribeSession(context))
      } catch (error: unknown) {
        result = {
          ...UNAVAILABLE_LIVE_RESULT,
          error,
          cancelled: isRpcAbort(error) || run.controller.signal.aborted,
        }
      }
      if (!isCurrent(run)) {
        return { ...result, authoritative: false, cancelled: true }
      }
      phase.result = result
      if (result.authoritative) {
        livePhase.value = 'ready'
        ownership.armRecovery()
      } else {
        livePhase.value = 'degraded'
      }
      return result
    })().finally(() => {
      phase.running = false
    })
    return phase.promise
  }

  function createRun(key: string, includeHistory: boolean): ActiveBootstrap {
    options.cancelHistory()
    options.cancelSubscription()
    const run = ownership.start(key, includeHistory, token => ({
      lease: options.sessionReadLifecycle.open({
        sessionKey: key,
        includeInitialHistory: includeHistory,
      }),
      history: historyRuntime(token.deadlineAt),
      live: liveRuntime(token.deadlineAt),
    }))
    active = run
    return run
  }

  function publicRun(run: ActiveBootstrap): SessionBootstrapRun {
    return {
      generation: run.generation,
      criticalRequestsQueued: run.lease.criticalRequestsQueued,
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
        generation: ownership.generation,
        criticalRequestsQueued: Promise.resolve(),
        history: Promise.resolve(EMPTY_HISTORY_RESULT),
        live: Promise.resolve(UNAVAILABLE_LIVE_RESULT),
      }
    }
    if (
      !optionsForStart.force
      && active
      && active.key === key
      && !active.controller.signal.aborted
      && leaseIsCurrent(active)
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
    // Opening the lease starts subscribe/snapshot and, when requested, the
    // eager latest-history frame. Both projections consume that same lease.
    run.live.promise = runLivePhase(run)
    run.history.promise = includeHistory
      ? runHistoryPhase(run, false)
      : Promise.resolve(EMPTY_HISTORY_RESULT)
    if (!includeHistory) historyPhase.value = 'ready'
    return publicRun(run)
  }

  function resetHistoryPhaseForRetry(run: ActiveBootstrap) {
    run.history = historyRuntime(Date.now() + SESSION_BOOTSTRAP_BUDGET_MS)
  }

  function retryHistory(): Promise<SessionPhaseResult> {
    const key = options.sessionKey.value
    const run = active
    if (
      !run
      || run.key !== key
      || run.controller.signal.aborted
      || !leaseIsCurrent(run)
    ) {
      return startSessionBootstrap({ includeHistory: true, force: true }).history
    }
    if (run.history.running) return run.history.promise
    resetHistoryPhaseForRetry(run)
    run.history.promise = runHistoryPhase(run, true)
    return run.history.promise
  }

  function retryLive(): Promise<SessionSubscriptionOutcome> {
    const run = active
    if (run && isCurrent(run) && leaseIsCurrent(run) && options.reconcileSession) {
      if (run.live.running) {
        if (queuedReconciliation?.run === run) return queuedReconciliation.promise
        // A gap arriving during an older read needs one fresh snapshot after
        // that read, not its pre-gap result as false recovery evidence.
        const pending = run.live.promise.then(() => {
          if (!isCurrent(run) || !leaseIsCurrent(run)) return UNAVAILABLE_LIVE_RESULT
          run.live = liveRuntime(Date.now() + SESSION_BOOTSTRAP_BUDGET_MS)
          return runLivePhase(run, true)
        })
        const observed = pending.finally(() => {
          if (queuedReconciliation?.promise === observed) queuedReconciliation = null
        })
        queuedReconciliation = { run, promise: observed }
        return observed
      }
      run.live = liveRuntime(Date.now() + SESSION_BOOTSTRAP_BUDGET_MS)
      return runLivePhase(run, true)
    }
    const priorHistoryPhase = historyPhase.value
    const replacement = startSessionBootstrap({ includeHistory: false, force: true })
    historyPhase.value = priorHistoryPhase
    return replacement.live
  }

  function cancelSessionBootstrap(unsubscribe = true) {
    clearRecoveryTimer()
    recoveryAttempt = 0
    const cancelled = ownership.cancel() ?? active
    active = null
    options.cancelHistory()
    options.cancelSubscription()
    historyPhase.value = 'idle'
    livePhase.value = 'idle'
    if (unsubscribe && cancelled) void cancelled.lease.close().catch(() => {})
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

  function currentSessionBootstrap(): SessionBootstrapRun | undefined {
    const run = active
    if (
      !run
      || !isCurrent(run)
      || !leaseIsCurrent(run)
    ) return
    return publicRun(run)
  }

  function handleConnectionState(
    state: string,
    includeHistory = true,
  ): SessionBootstrapRun | undefined {
    if (
      ownership.shouldDeferConnectionState(options.sessionKey.value)
    ) {
      // Transport events may race a delayed queue/adoption handoff. Keep the
      // source run intact and replay only the latest physical state after the
      // handoff commits or rolls back; never restart source A while B is the
      // declared target.
      ownership.deferConnectionState(state, includeHistory)
      return active
        ? { ...publicRun(active), deferred: true }
        : {
            generation: ownership.generation,
            criticalRequestsQueued: Promise.resolve(),
            history: Promise.resolve(EMPTY_HISTORY_RESULT),
            live: Promise.resolve(UNAVAILABLE_LIVE_RESULT),
            deferred: true,
          }
    }
    if (state === 'disconnected') {
      const key = options.sessionKey.value
      if (!key) return
      const run = active
      const currentRun = run
        && run.key === key
        && !run.controller.signal.aborted
      const inFlight = currentRun && (run.history.running || run.live.running)
      if (!inFlight && !ownership.consumeRecoveryBudget()) {
        return currentRun ? publicRun(run) : undefined
      }
      ownership.disarmRecovery()
      // Start the replacement immediately. The Adapter's bounded ready wait
      // owns the outage deadline; no physical queue state is rearmed here.
      return startSessionBootstrap({ includeHistory, force: true })
    }
    if (state !== 'connected' || !options.sessionKey.value) return

    const run = active
    if (!run || run.key !== options.sessionKey.value || run.controller.signal.aborted) {
      return startSessionBootstrap({ includeHistory, force: true })
    }
    if (!leaseIsCurrent(run)) {
      return startSessionBootstrap({ includeHistory, force: true })
    }
    if (run.live.running) {
      // Keep one public run internally coherent: generation, admission,
      // history and live must all belong to the same lease. A connected event
      // can race a healthy attempt and cause one extra lease, but that cost is
      // bounded by physical transport transitions. The handoff defer guard
      // above prevents replacing source A while target B is unresolved.
      return startSessionBootstrap({ includeHistory, force: true })
    }
    if (livePhase.value === 'ready') return publicRun(run)
    if (livePhase.value === 'degraded') {
      return startSessionBootstrap({ includeHistory, force: true })
    }
    return publicRun(run)
  }

  // Recovery is request-local. The shared socket and the current page remain
  // owned by their existing lifecycles; a missing session is never retried.
  const stopRecoveryWatch = options.connectionState ? watch(
    [options.sessionKey, options.connectionState, historyPhase, livePhase],
    () => {
      clearRecoveryTimer()
      const run = active
      if (!run || !isCurrent(run) || options.connectionState?.value !== 'connected') return
      const historyFailed = historyPhase.value === 'error'
        && shouldRetrySessionPhase(run.history.result?.error)
      const liveFailed = livePhase.value === 'degraded'
        && !run.live.result?.sessionMissing
        && shouldRetrySessionPhase(run.live.result?.error)
      if (!historyFailed && !liveFailed) {
        if (historyPhase.value === 'ready' && livePhase.value === 'ready') recoveryAttempt = 0
        return
      }
      const delayMs = Math.min(15_000, 500 * 2 ** Math.min(recoveryAttempt, 5))
      recoveryTimer = setTimeout(() => {
        recoveryTimer = null
        if (!isCurrent(run) || options.connectionState?.value !== 'connected') return
        recoveryAttempt++
        if (liveFailed) void retryLive().catch(() => {})
        if (historyFailed) void retryHistory().catch(() => {})
      }, delayMs)
    },
  ) : undefined
  if (getCurrentScope()) onScopeDispose(() => {
    stopRecoveryWatch?.()
    clearRecoveryTimer()
  })

  return {
    historyPhase,
    livePhase,
    startSessionBootstrap,
    cancelSessionBootstrap,
    retryHistory,
    retryLive,
    handleConnectionState,
    setSessionHandoffTarget,
    isSessionBootstrapCurrent,
    currentSessionBootstrap,
  }
}
