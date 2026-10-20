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
import {
  SessionReadFailure,
  type SessionReadLease,
  type SessionReadLifecycle,
} from '@/modules/sessionReadLifecycle'

type PhaseRuntime<T> = ConversationBootstrapPhase<T>

interface ActiveBootstrapState {
  lease: SessionReadLease | null
  leaseReady: Promise<SessionReadLease>
  criticalRequestsQueued: Promise<void>
  readonly incident: RecoveryIncident
  readonly scope: string
  acceptedScope?: string
  history: PhaseRuntime<SessionPhaseResult>
  live: PhaseRuntime<SessionSubscriptionOutcome>
}

type ActiveBootstrap = ConversationBootstrapRunToken & ActiveBootstrapState

const RECOVERY_BUDGET_MS = 120_000
interface RecoveryIncident {
  started: number
  degraded: boolean
  dismissed: boolean
  deadlineAt: number
  notBefore: number
  attempts: number
  recovered: boolean
  liveRecovered: boolean
}

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
  isProvisionalDraft?: () => boolean
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
  incidentScope?: () => string
  retryMetadata?: () => Promise<unknown>
  /** Deferred task metadata can fail independently of a healthy live ACK. */
  metadataRecoveryError?: Readonly<Ref<unknown>>
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
  const liveAttemptCompletion = ref(0)
  const retryBusy = ref(false)
  const noticeDismissed = ref(false)
  const incidents = new Map<string, RecoveryIncident>()
  let foregroundTimer: ReturnType<typeof setTimeout> | null = null
  function incidentKey(key: string) { return `${options.incidentScope?.() ?? ''}\0${key}` }
  function clearForegroundTimer() {
    if (foregroundTimer !== null) clearTimeout(foregroundTimer)
    foregroundTimer = null
  }
  function incidentFor(key: string) {
    const identity = incidentKey(key)
    let incident = incidents.get(identity)
    if (!incident) {
      incident = { started: performance.now(), degraded: false, dismissed: false,
        deadlineAt: Date.now() + RECOVERY_BUDGET_MS, notBefore: 0, attempts: 0, recovered: false, liveRecovered: false }
      incidents.set(identity, incident)
      while (incidents.size > 64) incidents.delete(incidents.keys().next().value!)
    }
    return incident
  }
  function beginRecovery(key: string, explicit = false, live = false): RecoveryIncident {
    const incident = incidentFor(key)
    if (explicit || incident.recovered || (live && incident.liveRecovered)) {
      if (incident.recovered || (live && incident.liveRecovered)) {
        incident.started = performance.now()
        incident.degraded = false
        incident.dismissed = false
      }
      incident.deadlineAt = Date.now() + RECOVERY_BUDGET_MS
      incident.attempts = 0
      incident.recovered = false
      // Renewing an explicit budget never waives a server's Retry-After.
    }
    if (live) incident.liveRecovered = false
    return incident
  }
  function exhausted(): SessionReadFailure {
    return new SessionReadFailure('budget-exhausted', 'Session read recovery budget exhausted.', false)
  }
  function noteRecoveryFailure(incident: RecoveryIncident, error: unknown): void {
    if (!shouldRetrySessionPhase(error)) return
    incident.recovered = false
    const delay = Math.max(retryAfterMs(error), Math.min(15_000, 500 * 2 ** Math.min(incident.attempts++, 5)))
    incident.notBefore = Math.max(incident.notBefore, Date.now() + delay)
  }
  async function waitForAdmission(run: ActiveBootstrap): Promise<void> {
    while (run.incident.notBefore > Date.now()) {
      if (!await waitBeforeRetry(new SessionReadFailure('busy', 'Session recovery is waiting.', true,
        run.incident.notBefore - Date.now()), run, run.incident.deadlineAt)) throw exhausted()
    }
    if (!isCurrent(run)) throw new SessionReadFailure('aborted', 'Session recovery owner changed.', false)
    if (Date.now() >= run.incident.deadlineAt) throw exhausted()
  }
  function dismissRecoveryNotice() {
    const incident = incidentFor(options.sessionKey.value)
    incident.dismissed = true
    noticeDismissed.value = true
  }
  let active: ActiveBootstrap | null = null
  let recoveryTimer: ReturnType<typeof setTimeout> | null = null
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
      && (!run.scope || run.scope === (options.incidentScope?.() ?? ''))
      && (run.acceptedScope === undefined || run.acceptedScope === (options.incidentScope?.() ?? ''))
      && (!run.lease || leaseIsCurrent(run))
  }

  function leaseIsCurrent(run: ActiveBootstrap): boolean {
    return run.lease !== null && options.sessionReadLifecycle.current() === run.lease
  }

  function isLiveReady(key = options.sessionKey.value): boolean {
    return Boolean(active && active.key === key && isCurrent(active) && leaseIsCurrent(active)
      && livePhase.value === 'ready' && active.live.result?.authoritative
      && !active.live.result.cancelled && active.acceptedScope === (options.incidentScope?.() ?? '')
      && (!options.connectionState || options.connectionState.value === 'connected'))
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
      try {
        if (!run.lease) await run.leaseReady
        if (retryFirst && (run.incident.notBefore > Date.now() || Date.now() >= run.incident.deadlineAt)) await waitForAdmission(run)
      } catch (error) { return { ok: false, error } }
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
          return lastResult
        }
        run.incident.notBefore = Math.max(run.incident.notBefore, Date.now() + retryAfterMs(lastResult.error))
        if (
          phase.attempts >= maxAttempts
          || !shouldRetrySessionPhase(lastResult.error)
          || !await waitBeforeRetry(lastResult.error, run, phase.deadlineAt)
        ) {
          break
        }
      }
      return lastResult
    })().then(result => {
      // Publish the failure evidence before the reactive phase. Vue can flush
      // the recovery watcher before a later Promise continuation; exposing
      // `error` while result is still null permanently misses its retry.
      phase.result = result
      if (isCurrent(run) && !result.cancelled) {
        if (!result.ok) noteRecoveryFailure(run.incident, result.error)
        historyPhase.value = result.ok ? 'ready' : 'error'
      }
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
    if (isCurrent(run)) {
      const incident = run.incident
      noticeDismissed.value = incident.dismissed
      const remaining = incident.started + SESSION_BOOTSTRAP_BUDGET_MS - performance.now()
      incident.degraded ||= remaining <= 0
      livePhase.value = incident.degraded ? 'degraded' : 'connecting'
      clearForegroundTimer()
      if (!incident.degraded) foregroundTimer = setTimeout(() => {
        foregroundTimer = null
        incident.degraded = true
        if (isCurrent(run) && livePhase.value !== 'ready') livePhase.value = 'degraded'
      }, remaining)
    }

    phase.attempts = 1
    phase.promise = (async () => {
      let result: SessionSubscriptionOutcome
      try {
        if (!run.lease) await run.leaseReady
        if (run.incident.notBefore > Date.now() || Date.now() >= run.incident.deadlineAt) await waitForAdmission(run)
        const controller = new AbortController()
        const cancel = () => controller.abort()
        run.controller.signal.addEventListener('abort', cancel, { once: true })
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          const context = { ...contextFor(run, phase, 0), signal: controller.signal }
          result = await Promise.race([
            reconcile && options.reconcileSession ? options.reconcileSession(context) : options.subscribeSession(context),
            new Promise<SessionSubscriptionOutcome>((_resolve, reject) => {
              timer = setTimeout(() => { controller.abort(); reject(exhausted()) },
                Math.max(0, run.incident.deadlineAt - Date.now()))
            }),
          ])
        } finally {
          clearTimeout(timer)
          run.controller.signal.removeEventListener('abort', cancel)
        }
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
      if (result.authoritative && !result.cancelled) {
        clearForegroundTimer()
        run.acceptedScope = options.incidentScope?.() ?? ''
        // The first Hello may bind authority after this same run began.
        // Keep its incident under that identity instead of creating a fresh
        // budget when a later recovery asks for the current scope.
        const previousIdentity = `${run.scope}\0${run.key}`
        if (previousIdentity !== incidentKey(run.key) && incidents.get(previousIdentity) === run.incident) {
          incidents.delete(previousIdentity)
        }
        incidents.set(incidentKey(run.key), run.incident)
        noticeDismissed.value = false
        livePhase.value = 'ready'
        // Optional metadata failures cannot spend the recovery allowance
        // earned by a genuinely installed live session.
        run.incident.liveRecovered = true
        ownership.armRecovery()
      } else {
        noteRecoveryFailure(run.incident, result.error)
        run.incident.degraded = true
        clearForegroundTimer()
        livePhase.value = 'degraded'
      }
      return result
    })().finally(() => {
      phase.running = false
      if (isCurrent(run)) liveAttemptCompletion.value++
    })
    return phase.promise
  }

  function createRun(
    key: string,
    includeHistory: boolean,
    liveDeadlineAt?: number,
    retainedIncident?: RecoveryIncident,
  ): ActiveBootstrap {
    options.cancelHistory()
    options.cancelSubscription()
    const incident = retainedIncident ?? beginRecovery(key, false, true)
    if (retainedIncident) {
      incident.recovered = false
      incident.liveRecovered = false
    }
    if (liveDeadlineAt !== undefined) incident.deadlineAt = Math.min(incident.deadlineAt, liveDeadlineAt)
    const run = ownership.start(key, includeHistory, (token): ActiveBootstrapState => ({
      lease: null,
      leaseReady: Promise.resolve(null as unknown as SessionReadLease),
      criticalRequestsQueued: Promise.resolve(),
      incident,
      scope: options.incidentScope?.() ?? '',
      history: historyRuntime(Math.min(incident.deadlineAt,
        Math.max(token.deadlineAt, incident.notBefore + SESSION_BOOTSTRAP_BUDGET_MS))),
      live: liveRuntime(incident.deadlineAt),
    }))
    active = run
    const open = () => {
      if (!isCurrent(run)) throw new SessionReadFailure('aborted', 'Session recovery owner changed.', false)
      if (Date.now() >= incident.deadlineAt) throw exhausted()
      return run.lease = options.sessionReadLifecycle.open({
        sessionKey: key,
        ...(options.isProvisionalDraft?.() ? { provisionalDraft: true } : {}),
        includeInitialHistory: includeHistory,
      })
    }
    // Replacing a lease is still part of this incident, not a new retry budget.
    try {
      run.leaseReady = incident.notBefore > Date.now()
        ? waitForAdmission(run).then(open) : Promise.resolve(open())
    } catch (error) { run.leaseReady = Promise.reject(error) }
    run.criticalRequestsQueued = run.lease?.criticalRequestsQueued
      ?? run.leaseReady.then(lease => lease.criticalRequestsQueued)
    void run.criticalRequestsQueued.catch(() => {})
    return run
  }

  function publicRun(run: ActiveBootstrap): SessionBootstrapRun {
    return {
      generation: run.generation,
      criticalRequestsQueued: run.criticalRequestsQueued,
      history: run.history.promise,
      live: run.live.promise,
    }
  }

  function installRun(run: ActiveBootstrap, includeHistory: boolean): SessionBootstrapRun {
    // Opening the lease starts subscribe/snapshot and, when requested, the
    // eager latest-history frame. Both projections consume that same lease.
    run.live.promise = runLivePhase(run)
    run.history.promise = includeHistory
      ? runHistoryPhase(run, false)
      : Promise.resolve(EMPTY_HISTORY_RESULT)
    if (!includeHistory) historyPhase.value = 'ready'
    return publicRun(run)
  }

  function startSessionBootstrap(optionsForStart: {
    includeHistory?: boolean
    force?: boolean
    liveDeadlineAt?: number
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
      && isCurrent(active)
      && (!active.lease || leaseIsCurrent(active))
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

    return installRun(createRun(key, includeHistory, optionsForStart.liveDeadlineAt), includeHistory)
  }

  function resetHistoryPhaseForRetry(run: ActiveBootstrap) {
    run.history = historyRuntime(Math.min(run.incident.deadlineAt,
      Math.max(Date.now(), run.incident.notBefore) + SESSION_BOOTSTRAP_BUDGET_MS))
  }

  function retryHistory(explicit = true): Promise<SessionPhaseResult> {
    const key = options.sessionKey.value
    const run = active
    if (
      !run
      || run.key !== key
      || run.controller.signal.aborted
      || !leaseIsCurrent(run)
    ) {
      if (explicit) beginRecovery(key, true)
      return startSessionBootstrap({ includeHistory: true, force: true }).history
    }
    if (run.history.running) return run.history.promise
    beginRecovery(key, explicit)
    resetHistoryPhaseForRetry(run)
    run.history.promise = runHistoryPhase(run, true)
    return run.history.promise
  }

  function retryLive(explicit = false): Promise<SessionSubscriptionOutcome> {
    const run = active
    if (explicit && run && isCurrent(run)) {
      if (run.live.running) return run.live.promise
      beginRecovery(run.key, true, true)
      if (run.live.result?.error && !shouldRetrySessionPhase(run.live.result.error)) {
        const previousHistoryPhase = historyPhase.value
        const includeHistory = previousHistoryPhase !== 'ready'
        const replacement = startSessionBootstrap({ includeHistory, force: true })
        if (!includeHistory) historyPhase.value = previousHistoryPhase
        return replacement.live
      }
    }
    if (run && isCurrent(run) && !run.live.running) beginRecovery(run.key, false, true)
    if (run && isCurrent(run) && leaseIsCurrent(run) && !run.live.running
      && run.live.result?.error instanceof SessionReadFailure
      && run.live.result.error.kind === 'rebase-required') {
      const deadlineAt = run.incident.deadlineAt
      if (Date.now() >= deadlineAt) {
        const result = { ...run.live.result, error: new SessionReadFailure(
          'budget-exhausted', 'Session read recovery budget exhausted.', false, 0, run.live.result.error,
        ) }
        run.live.result = result
        liveAttemptCompletion.value++
        return Promise.resolve(result)
      }
      // The existing owner retires the exact old lease before opening the new
      // base. Keep history and the incident deadline across read-only rebases.
      const previousHistoryPhase = historyPhase.value
      const includeHistory = previousHistoryPhase !== 'ready'
      const replacement = startSessionBootstrap({ includeHistory, force: true, liveDeadlineAt: deadlineAt })
      if (!includeHistory) historyPhase.value = previousHistoryPhase
      return replacement.live
    }
    if (run && isCurrent(run) && leaseIsCurrent(run) && options.reconcileSession) {
      if (run.live.running) {
        if (queuedReconciliation?.run === run) return queuedReconciliation.promise
        // A gap arriving during an older read needs one fresh snapshot after
        // that read, not its pre-gap result as false recovery evidence.
        const pending = run.live.promise.then(() => {
          if (!isCurrent(run) || !leaseIsCurrent(run)) return UNAVAILABLE_LIVE_RESULT
          beginRecovery(run.key, false, true)
          run.live = liveRuntime(run.incident.deadlineAt)
          return runLivePhase(run, true)
        })
        const observed = pending.finally(() => {
          if (queuedReconciliation?.promise === observed) queuedReconciliation = null
        })
        queuedReconciliation = { run, promise: observed }
        return observed
      }
      run.live = liveRuntime(run.incident.deadlineAt)
      return runLivePhase(run, true)
    }
    const priorHistoryPhase = historyPhase.value
    const replacement = startSessionBootstrap({ includeHistory: false, force: true })
    historyPhase.value = priorHistoryPhase
    return replacement.live
  }

  function cancelSessionBootstrap(unsubscribe = true) {
    clearRecoveryTimer()
    clearForegroundTimer()
    const cancelled = ownership.cancel() ?? active
    active = null
    options.cancelHistory()
    options.cancelSubscription()
    historyPhase.value = 'idle'
    livePhase.value = 'idle'
    if (unsubscribe && cancelled?.lease) void cancelled.lease.close().catch(() => {})
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
      || (run.lease !== null && !leaseIsCurrent(run))
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
    const scope = options.incidentScope?.() ?? ''
    if ((run.scope && run.scope !== scope)
      || (run.acceptedScope !== undefined && run.acceptedScope !== scope)) {
      // A late first identity (or a changed authority) invalidates the old
      // proof even while the socket stays connected. An unfinished recovery
      // keeps its allowance; a healthy predecessor starts a new bounded
      // incident. Both retain the existing Retry-After.
      for (const [identity, incident] of incidents) {
        if (incident === run.incident) incidents.delete(identity)
      }
      incidents.set(incidentKey(run.key), run.incident)
      return installRun(createRun(run.key, includeHistory, undefined,
        run.incident.liveRecovered ? undefined : run.incident), includeHistory)
    }
    if (run.lease && !leaseIsCurrent(run)) {
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
  let metadataRetryRunning = false
  let recordedMetadataError: unknown
  const stopRecoveryWatch = options.connectionState ? watch(
    [options.sessionKey, options.connectionState, historyPhase, livePhase, liveAttemptCompletion,
      () => options.metadataRecoveryError?.value, () => options.incidentScope?.()],
    () => {
      clearRecoveryTimer()
      const run = active
      if (!run || options.connectionState?.value !== 'connected') return
      const scope = options.incidentScope?.() ?? ''
      if ((run.scope && run.scope !== scope)
        || (run.acceptedScope !== undefined && run.acceptedScope !== scope)) {
        handleConnectionState('connected', run.includeHistory)
        return
      }
      if (!isCurrent(run)) return
      const historyFailed = historyPhase.value === 'error'
        && shouldRetrySessionPhase(run.history.result?.error)
      const metadataFailed = livePhase.value === 'ready'
        && shouldRetrySessionPhase(options.metadataRecoveryError?.value)
      const liveFailed = (livePhase.value === 'degraded'
        && !run.live.result?.sessionMissing
        && shouldRetrySessionPhase(run.live.result?.error))
      if (!historyFailed && !liveFailed && !metadataFailed) {
        if (historyPhase.value === 'ready' && livePhase.value === 'ready') {
          run.incident.recovered = true
          recordedMetadataError = undefined
        }
        return
      }
      if (metadataRetryRunning) return
      if (metadataFailed && options.metadataRecoveryError?.value !== recordedMetadataError) {
        beginRecovery(run.key)
        recordedMetadataError = options.metadataRecoveryError?.value
        noteRecoveryFailure(run.incident, recordedMetadataError)
      }
      if (Date.now() >= run.incident.deadlineAt) return
      const delayMs = Math.min(run.incident.deadlineAt - Date.now(),
        Math.max(0, run.incident.notBefore - Date.now()))
      recoveryTimer = setTimeout(() => {
        recoveryTimer = null
        if (!isCurrent(run) || options.connectionState?.value !== 'connected') return
        if (Date.now() >= run.incident.deadlineAt) return
        if (liveFailed) void retryLive().catch(() => {})
        if (metadataFailed) {
          metadataRetryRunning = true
          void Promise.resolve(options.retryMetadata?.()).catch(() => {}).finally(() => {
            metadataRetryRunning = false
            if (!isCurrent(run)) return
            recordedMetadataError = options.metadataRecoveryError?.value
            noteRecoveryFailure(run.incident, recordedMetadataError)
            liveAttemptCompletion.value++
          })
        }
        if (historyFailed) void retryHistory(false).catch(() => {})
      }, delayMs)
    },
  ) : undefined
  if (getCurrentScope()) onScopeDispose(() => {
    stopRecoveryWatch?.()
    clearRecoveryTimer()
    clearForegroundTimer()
  })

  return {
    historyPhase,
    livePhase,
    startSessionBootstrap,
    cancelSessionBootstrap,
    retryHistory,
    retryLive: (explicit = false) => {
      const work = retryLive(explicit)
      if (explicit) {
        retryBusy.value = true
        void work.finally(() => { retryBusy.value = false }).catch(() => {})
      }
      return work
    },
    retryBusy,
    noticeDismissed,
    dismissRecoveryNotice,
    handleConnectionState,
    setSessionHandoffTarget,
    isSessionBootstrapCurrent,
    currentSessionBootstrap,
    isLiveReady,
    ensureLiveReady: async (key = options.sessionKey.value): Promise<boolean> => {
      if (key !== options.sessionKey.value) return false
      if (isLiveReady(key)) return true
      const pending = active && isCurrent(active) && active.live.running
        ? active.live.promise : retryLive(false)
      const result = await pending
      return result.authoritative && !result.cancelled && isLiveReady(key)
    },
  }
}
