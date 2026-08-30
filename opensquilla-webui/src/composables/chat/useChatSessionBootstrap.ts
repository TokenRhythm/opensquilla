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
import {
  createConversationBootstrapCriticalQueue,
  createConversationBootstrapPhase,
  createConversationBootstrapCoordinator,
  rearmConversationBootstrapCriticalQueue,
  type ConversationBootstrapPhase,
  type ConversationBootstrapCriticalQueue,
  type ConversationBootstrapHandoffOutcome,
  type ConversationBootstrapRunToken,
  waitForConversationBootstrapRetry,
} from '@/modules/conversationBootstrapCoordinator'

type PhaseRuntime<T> = ConversationBootstrapPhase<T>

interface ActiveBootstrapState {
  criticalQueue: ConversationBootstrapCriticalQueue
  freshLiveOutageForHistoryRetry: boolean
  awaitingReplacementConnection: boolean
  lateReplacementRecoveryUsed: boolean
  lateReplacementHistoryRecoveryPhase: PhaseRuntime<SessionPhaseResult> | null
  lateReplacementHistoryRecoveryUsed: boolean
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
  return createConversationBootstrapPhase(deadlineAt, EMPTY_HISTORY_RESULT)
}

function liveRuntime(deadlineAt: number): PhaseRuntime<SessionSubscriptionOutcome> {
  return createConversationBootstrapPhase(deadlineAt, UNAVAILABLE_LIVE_RESULT)
}

export function useChatSessionBootstrap(options: UseChatSessionBootstrapOptions) {
  const historyPhase = ref<SessionHistoryPhase>('idle')
  const livePhase = ref<SessionLivePhase>('idle')
  let active: ActiveBootstrap | null = null
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
            markLiveSubscribeSent: (socketGeneration: number) =>
              markLiveSubscribeSent(run, socketGeneration),
            waitForCriticalRequestsQueued: () => run.criticalQueue.promise,
          }
        : {
            markHistoryRequestSent: (socketGeneration: number) =>
              markHistoryRequestSent(run, socketGeneration),
          }),
    }
  }

  function markLiveSubscribeSent(
    run: ActiveBootstrap,
    socketGeneration: number,
  ) {
    if (!isCurrent(run)) return
    run.criticalQueue.markLiveSubscribeSent(socketGeneration)
    tryRecoverHistoryOnLateReplacement(run)
  }

  function markHistoryRequestSent(
    run: ActiveBootstrap,
    socketGeneration: number,
  ) {
    if (!isCurrent(run)) return
    run.criticalQueue.markHistoryRequestSent(socketGeneration)
  }

  function requiresFreshLiveQueue(error: unknown): boolean {
    const message = error instanceof Error ? error.message.toLowerCase() : ''
    return (
      message.includes('connection')
      || message.includes('socket')
      || message.includes('not connected')
      || message.includes('network')
    )
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
        error: new RpcTimeoutError('chat.history', 0),
      }
      let requiredLiveQueueSequence = Math.max(1, run.criticalQueue.liveQueueSequence)
      while (phase.attempts < maxAttempts && isCurrent(run)) {
        if (Date.now() >= phase.deadlineAt) break
        if (!await run.criticalQueue.waitForLiveSubscribeSent(
          requiredLiveQueueSequence,
          phase.deadlineAt,
          run.controller.signal,
          () => isCurrent(run),
        )) {
          break
        }
        const liveQueueSequenceForAttempt = run.criticalQueue.liveQueueSequence
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
      // A disconnected or exhausted phase may terminate before it can send.
      // Optional UI traffic must not remain globally blocked in that case.
      run.criticalQueue.markHistoryTerminal()
      phase.running = false
      tryRecoverHistoryOnLateReplacement(run)
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
          ownership.armRecovery()
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
      // Match the history fallback above: failure to queue a critical request
      // is terminal for this attempt, not a reason to freeze the whole app.
      run.criticalQueue.markLiveTerminal()
      phase.running = false
    })
    return phase.promise
  }

  function createRun(key: string, includeHistory: boolean): ActiveBootstrap {
    const run = ownership.start(key, includeHistory, token => ({
      criticalQueue: createConversationBootstrapCriticalQueue(includeHistory),
      freshLiveOutageForHistoryRetry: false,
      awaitingReplacementConnection: false,
      lateReplacementRecoveryUsed: false,
      lateReplacementHistoryRecoveryPhase: null,
      lateReplacementHistoryRecoveryUsed: false,
      history: historyRuntime(token.deadlineAt),
      live: liveRuntime(token.deadlineAt),
    }))
    active = run
    return run
  }

  function publicRun(run: ActiveBootstrap): SessionBootstrapRun {
    return {
      generation: run.generation,
      criticalRequestsQueued: run.criticalQueue.promise,
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
    ) {
      if (includeHistory && !active.includeHistory) {
        if (Date.now() >= active.history.deadlineAt) {
          return startSessionBootstrap({ includeHistory: true, force: true })
        }
        const liveSocketGeneration =
          active.criticalQueue.liveSocketGeneration
        active.includeHistory = true
        active.criticalQueue = rearmConversationBootstrapCriticalQueue(
          active.criticalQueue,
          true,
          liveSocketGeneration,
        )
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

  function resetHistoryPhaseForRetry(run: ActiveBootstrap) {
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
    resetHistoryPhaseForRetry(run)
    // A user-initiated history retry is a new recovery operation. If its local
    // timeout recycles an otherwise-authoritative live socket, re-register live
    // with a fresh outage budget instead of inheriting exhausted attempts from
    // the original bootstrap.
    run.freshLiveOutageForHistoryRetry = true
    run.history.promise = runHistoryPhase(run, true)
    return run.history.promise
  }

  function armHistoryRecoveryForLateReplacement(run: ActiveBootstrap) {
    if (
      !isCurrent(run)
      || !run.includeHistory
      || run.lateReplacementHistoryRecoveryUsed
    ) return
    run.lateReplacementHistoryRecoveryPhase ??= run.history
    tryRecoverHistoryOnLateReplacement(run)
  }

  function tryRecoverHistoryOnLateReplacement(run: ActiveBootstrap) {
    const phase = run.lateReplacementHistoryRecoveryPhase
    if (
      !phase
      || run.lateReplacementHistoryRecoveryUsed
      || !isCurrent(run)
    ) return
    if (run.history !== phase) {
      run.lateReplacementHistoryRecoveryPhase = null
      return
    }
    if (phase.running || historyPhase.value !== 'error' || !phase.result) return
    const terminal = phase.result
    const recoverable = (
      !terminal.ok
      && !terminal.cancelled
      && (
        isRpcTimeout(terminal.error)
        || requiresFreshLiveQueue(terminal.error)
      )
    )
    if (!recoverable) {
      run.lateReplacementHistoryRecoveryPhase = null
      return
    }
    const liveSocketGeneration = run.criticalQueue.liveSocketGeneration
    if (liveSocketGeneration === null) return

    run.lateReplacementHistoryRecoveryUsed = true
    run.lateReplacementHistoryRecoveryPhase = null
    resetHistoryPhaseForRetry(run)
    run.criticalQueue = rearmConversationBootstrapCriticalQueue(
      run.criticalQueue,
      true,
      liveSocketGeneration,
    )
    run.history.promise = runHistoryPhase(run, true, 1)
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
    const cancelled = ownership.cancel() ?? active
    active = null
    if (cancelled) {
      cancelled.criticalQueue.cancel()
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
      if (currentRun && (run.history.running || run.live.running)) {
        const liveWasReady = livePhase.value === 'ready'
        const liveWillRecover = run.live.running || liveWasReady
        if (liveWillRecover) {
          run.awaitingReplacementConnection = true
          run.criticalQueue = rearmConversationBootstrapCriticalQueue(
            run.criticalQueue,
            run.includeHistory && run.history.running,
          )
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
        if (liveWillRecover) ownership.disarmRecovery()
        return publicRun(run)
      }
      // Once a recovery budget reaches a terminal degraded state, background
      // reconnect churn must not turn the honest terminal state back into an
      // endless "connecting" indicator. Only an authoritative live phase can
      // arm a fresh outage budget.
      if (!ownership.consumeRecoveryBudget()) {
        return currentRun ? publicRun(run) : undefined
      }
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
    const replacementConnected = run.awaitingReplacementConnection
    run.awaitingReplacementConnection = false
    if (replacementConnected) armHistoryRecoveryForLateReplacement(run)
    if (run.live.running) {
      const interruptedPhase = run.live
      const resumeOnReplacement = (outcome?: SessionSubscriptionOutcome) => {
        const transportFailedAfterConnected = (
          !replacementConnected
          && requiresFreshLiveQueue(outcome?.error)
        )
        if (
          (!replacementConnected && !transportFailedAfterConnected)
          || !isCurrent(run)
          || run.live !== interruptedPhase
          || interruptedPhase.running
          || run.lateReplacementRecoveryUsed
          || (
            livePhase.value !== 'connecting'
            && livePhase.value !== 'degraded'
          )
        ) return
        run.lateReplacementRecoveryUsed = true
        run.criticalQueue = rearmConversationBootstrapCriticalQueue(
          run.criticalQueue,
          false,
        )
        // This is a continuation of the same outage, not a user-initiated
        // retry. Grant exactly one attempt on the authenticated socket. The
        // connected event can win a route-switch race before the new run sees
        // the matching disconnected event, so the interrupted phase may have
        // already consumed both of its attempts on the retired generation.
        // lateReplacementRecoveryUsed prevents later socket churn from
        // repeatedly extending this recovery window.
        run.live = {
          ...liveRuntime(interruptedPhase.deadlineAt),
          attempts: 1,
          skipSnapshot: interruptedPhase.skipSnapshot,
        }
        run.live.promise = runLivePhase(run)
        if (transportFailedAfterConnected) {
          armHistoryRecoveryForLateReplacement(run)
        }
      }
      // The replacement handshake can finish before the interrupted subscribe
      // observes its cancellation. Resume exactly once after that old phase
      // settles instead of leaving the UI indefinitely in "connecting".
      void interruptedPhase.promise.then(
        resumeOnReplacement,
        () => resumeOnReplacement(),
      )
      return publicRun(run)
    }
    if (!run.live.running && livePhase.value === 'degraded') {
      if (run.lateReplacementRecoveryUsed) return publicRun(run)
      run.lateReplacementRecoveryUsed = true
      // A replacement socket is a new recovery opportunity, even when the
      // previous socket exhausted its bounded subscribe attempts. RpcClient
      // owns the process-wide 1/2/4/8/15 second connection backoff; once its
      // handshake succeeds, immediately register this Session on that socket.
      // Keep an independently terminal history phase intact: restarting the
      // whole bootstrap here can hide its actionable error behind a fresh
      // loading state while replacement sockets continue to arrive.
      run.criticalQueue = rearmConversationBootstrapCriticalQueue(
        run.criticalQueue,
        false,
      )
      resetLivePhaseForManualRetry(run)
      run.live.promise = runLivePhase(run)
      armHistoryRecoveryForLateReplacement(run)
      return publicRun(run)
    }
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
    setSessionHandoffTarget,
    isSessionBootstrapCurrent,
  }
}
