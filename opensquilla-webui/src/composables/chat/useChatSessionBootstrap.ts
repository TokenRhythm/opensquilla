import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'

import type { SessionSubscriptionOutcome, SessionMetadataState, SessionMetadataOutcome,
  SessionMetadataRetryOptions } from '@/composables/chat/useChatSessionSubscription'
import { createSessionMetadataRecovery } from './createSessionMetadataRecovery'
import type { SessionLiveReadiness, SessionRecoveryProof } from '@/composables/chat/sessionRecoveryProof'
import {
  SESSION_BOOTSTRAP_BUDGET_MS,
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  SESSION_RECOVERY_BUDGET_MS,
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
  scope: string
  lease: SessionReadLease | null
  admission: Promise<void>
  criticalRequestsQueued: Promise<void>
  incident: RecoveryIncident
  history: PhaseRuntime<SessionPhaseResult>
  live: PhaseRuntime<SessionSubscriptionOutcome> & { readAttempt: number }
}

type ActiveBootstrap = ConversationBootstrapRunToken & ActiveBootstrapState

interface RecoveryIncident {
  started: number
  expiresAt: number
  deadlineAt: number
  notBefore: number
  attempts: number
  degraded: boolean
  dismissed: boolean
  terminal: SessionSubscriptionOutcome | null
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
  metadata?: {
    state: Readonly<Ref<SessionMetadataState>>
    runAttempt: (options: SessionMetadataRetryOptions) => Promise<SessionMetadataOutcome>
    cancelAttempt: () => void
  }
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

function liveRuntime(deadlineAt: number): ActiveBootstrapState['live'] {
  return { ...createConversationBootstrapPhase(deadlineAt, UNAVAILABLE_LIVE_RESULT), readAttempt: 0 }
}

export function useChatSessionBootstrap(options: UseChatSessionBootstrapOptions) {
  const historyPhase = ref<SessionHistoryPhase>('idle')
  const livePhase = ref<SessionLivePhase>('idle')
  const liveAttemptCompletion = ref(0)
  const liveRecoveryTerminal = ref(false)
  const retryBusy = ref(false)
  const sessionRetryBusy = ref(false)
  let sessionRetry: { isCurrent: () => boolean; controller: AbortController; promise: Promise<boolean> } | null = null
  function cancelSessionRetry() {
    sessionRetry?.controller.abort()
    sessionRetry = null
    sessionRetryBusy.value = false
  }
  const noticeDismissed = ref(false)
  const proofRevision = ref(0)
  let liveReadAttempt = 0
  let proofOwner = {}
  let proofKey = options.sessionKey.value
  let proofScope = options.incidentScope?.() ?? ''
  let proofAuthorityObserved = Boolean(proofScope)
  let disposed = false
  function syncProofOwner() {
    const key = options.sessionKey.value
    const scope = options.incidentScope?.() ?? ''
    // Only the first unknown-to-proven binding preserves continuity. Observe
    // every authority transition, even when A never completes a successful read.
    if (key !== proofKey || scope !== proofScope && (proofAuthorityObserved || !scope)) proofOwner = {}
    proofAuthorityObserved ||= Boolean(scope)
    proofKey = key
    proofScope = scope
  }
  const stopProofWatch = watch([options.sessionKey, () => options.incidentScope?.() ?? ''], () => {
    syncProofOwner()
    if (sessionRetry && !sessionRetry.isCurrent()) cancelSessionRetry()
    proofRevision.value++
  }, { flush: 'sync' })
  const recoveryProof: SessionRecoveryProof = {
    revision: proofRevision,
    capture() {
      syncProofOwner()
      const owner = proofOwner
      const key = proofKey
      const floor = liveReadAttempt
      const isCurrentOwner = () => {
        syncProofOwner()
        return !disposed && owner === proofOwner && Boolean(key) && key === options.sessionKey.value
      }
      return Object.assign(() => {
        const run = active
        return Boolean(isCurrentOwner()
          && run?.key === key && run.live.readAttempt > floor
          && (!options.incidentScope || run.scope)
          && run.live.result && isCurrentLiveResult(run.live.result))
      }, { isCurrent: isCurrentOwner })
    },
  }
  const incidents = new Map<string, RecoveryIncident>()
  let foregroundTimer: ReturnType<typeof setTimeout> | null = null
  let incidentTimer: ReturnType<typeof setTimeout> | null = null
  let hasProvenAuthority = Boolean(options.incidentScope?.())
  function incidentKey(key: string) { return `${options.incidentScope?.() ?? ''}\0${key}` }
  function clearForegroundTimer() {
    if (foregroundTimer !== null) clearTimeout(foregroundTimer)
    foregroundTimer = null
  }
  function clearIncidentTimer() {
    if (incidentTimer !== null) clearTimeout(incidentTimer)
    incidentTimer = null
  }
  function incidentFor(key: string) {
    const identity = incidentKey(key)
    let incident = incidents.get(identity)
    if (!incident) {
      incident = {
        started: performance.now(), expiresAt: performance.now() + SESSION_RECOVERY_BUDGET_MS,
        deadlineAt: Date.now() + SESSION_RECOVERY_BUDGET_MS, notBefore: 0, attempts: 0,
        degraded: false, dismissed: false, terminal: null,
      }
      incidents.set(identity, incident)
      while (incidents.size > 64) incidents.delete(incidents.keys().next().value!)
    }
    return incident
  }
  function dismissRecoveryNotice() {
    const incident = incidentFor(options.sessionKey.value)
    incident.dismissed = true
    noticeDismissed.value = true
  }
  let active: ActiveBootstrap | null = null
  let recoveryTimer: ReturnType<typeof setTimeout> | null = null
  let recoveryAttempt = 0
  let queuedReconciliation: {
    owner: ActiveBootstrap
    incident: RecoveryIncident
    waitingForPredecessor: boolean
    needsFresh: boolean
    promise: Promise<SessionSubscriptionOutcome>
  } | null = null
  function clearRecoveryTimer() {
    if (recoveryTimer !== null) clearTimeout(recoveryTimer)
    recoveryTimer = null
  }
  const ownership = createConversationBootstrapCoordinator<ActiveBootstrap>({
    budgetMs: SESSION_BOOTSTRAP_BUDGET_MS,
  })
  const metadataRecovery = options.metadata ? createSessionMetadataRecovery({
    sessionKey: options.sessionKey,
    authority: () => options.incidentScope?.() ?? '',
    state: options.metadata.state,
    connectionState: options.connectionState,
    livePhase,
    currentLease: () => options.sessionReadLifecycle.current(),
    canRetry: () => Boolean(active?.live.result && isCurrentLiveResult(active.live.result)),
    runAttempt: options.metadata.runAttempt,
    cancelAttempt: options.metadata.cancelAttempt,
  }) : undefined

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
      && run.scope === (options.incidentScope?.() ?? '')
  }

  function leaseIsCurrent(run: ActiveBootstrap): boolean {
    return run.lease !== null && options.sessionReadLifecycle.current() === run.lease
  }

  function bindInitialAuthority(run: ActiveBootstrap): boolean {
    const scope = options.incidentScope?.() ?? ''
    if (hasProvenAuthority || run.scope || !scope
      || !ownership.isCurrent(run, options.sessionKey.value)) return false
    // Session reads await rpc.ready() and capture the admitted connection
    // generation. Hello authentication precedes readiness, while the hashed
    // target identity can arrive later. Bind that first delivery proof without
    // cancelling its already admitted snapshot or renewing its recovery budget.
    if (incidents.get(`\0${run.key}`) === run.incident) {
      incidents.delete(`\0${run.key}`)
      incidents.set(`${scope}\0${run.key}`, run.incident)
    }
    run.scope = scope
    hasProvenAuthority = true
    // A read can already be ready when its first identity arrives. Publish
    // proof availability even though livePhase has no new ready edge.
    proofRevision.value++
    return true
  }

  function exhausted(incident: RecoveryIncident): SessionSubscriptionOutcome {
    incident.terminal ??= {
      ...UNAVAILABLE_LIVE_RESULT,
      error: new SessionReadFailure('budget-exhausted', 'Session recovery deadline elapsed.', false),
    }
    return incident.terminal
  }

  function incidentResult(incident: RecoveryIncident): SessionSubscriptionOutcome | null {
    return incident.terminal ?? (performance.now() >= incident.expiresAt ? exhausted(incident) : null)
  }

  function publishTerminal(run: ActiveBootstrap, result: SessionSubscriptionOutcome) {
    if (!isCurrent(run)) return
    run.live.result = result
    liveRecoveryTerminal.value = true
    livePhase.value = 'degraded'
    clearForegroundTimer()
  }

  function armIncidentTimer(run: ActiveBootstrap) {
    clearIncidentTimer()
    incidentTimer = setTimeout(() => {
      incidentTimer = null
      if (isCurrent(run)) publishTerminal(run, exhausted(run.incident))
    }, Math.max(0, run.incident.expiresAt - performance.now()))
  }

  function renewIncident(incident: RecoveryIncident) {
    incident.started = performance.now()
    incident.expiresAt = incident.started + SESSION_RECOVERY_BUDGET_MS
    incident.deadlineAt = Date.now() + SESSION_RECOVERY_BUDGET_MS
    incident.attempts = 0
    incident.terminal = null
    incident.dismissed = false
    // An explicit retry grants a new total budget, not permission to ignore
    // a server's still-applicable retry hint (notBefore is deliberately kept).
    liveRecoveryTerminal.value = false
    if (active && isCurrent(active) && active.incident === incident) armIncidentTimer(active)
  }

  function waitForLiveAdmission(run: ActiveBootstrap): Promise<boolean> {
    const incident = run.incident
    const waitMs = Math.min(incident.notBefore, incident.expiresAt) - performance.now()
    if (waitMs <= 0) return Promise.resolve(isCurrent(run))
    return new Promise(resolve => {
      const finish = (ready: boolean) => {
        clearTimeout(timer)
        run.controller.signal.removeEventListener('abort', aborted)
        resolve(ready)
      }
      const aborted = () => finish(false)
      const timer = setTimeout(() => finish(isCurrent(run)), waitMs)
      run.controller.signal.addEventListener('abort', aborted, { once: true })
      if (run.controller.signal.aborted) aborted()
    })
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
      if (!run.lease) await run.admission
      let lastResult: SessionPhaseResult = {
        ok: false,
        error: new Error('The session history deadline elapsed before the first attempt.'),
      }
      while (phase.attempts < maxAttempts && isCurrent(run) && run.lease) {
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
    const incident = run.incident
    const terminal = incidentResult(incident)
    if (terminal) {
      publishTerminal(run, terminal)
      return Promise.resolve(terminal)
    }
    phase.running = true
    phase.result = null
    // Stamp the phase before admission: an already pending phase must never
    // certify an invalidation that arrives while it is waiting to read.
    phase.readAttempt = ++liveReadAttempt
    if (isCurrent(run)) {
      liveRecoveryTerminal.value = false
      armIncidentTimer(run)
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
      const attemptController = new AbortController()
      let cancel!: (result: SessionSubscriptionOutcome) => void
      const cancelled = new Promise<SessionSubscriptionOutcome>(resolve => { cancel = resolve })
      const abortAttempt = () => {
        attemptController.abort()
        cancel({ ...UNAVAILABLE_LIVE_RESULT, cancelled: true })
      }
      run.controller.signal.addEventListener('abort', abortAttempt, { once: true })
      let expire!: () => void
      const expired = new Promise<SessionSubscriptionOutcome>(resolve => {
        expire = () => {
          // Invalidate this projection before a late response can publish it.
          // The adapter retains responsibility for physical RPC cleanup.
          attemptController.abort()
          resolve(exhausted(incident))
        }
      })
      const deadlineTimer = setTimeout(expire, Math.max(0, incident.expiresAt - performance.now()))
      let result: SessionSubscriptionOutcome
      try {
        const read = async () => {
          if (!run.lease) await run.admission
          if (!isCurrent(run)) return { ...UNAVAILABLE_LIVE_RESULT, cancelled: true }
          const blocked = incidentResult(incident)
          if (blocked) return blocked
          const boundedContext = { ...contextFor(run, phase, 0), signal: attemptController.signal }
          return reconcile && options.reconcileSession
            ? options.reconcileSession(boundedContext)
            : options.subscribeSession(boundedContext)
        }
        result = await Promise.race([
          read(),
          expired,
          cancelled,
        ])
      } catch (error: unknown) {
        result = {
          ...UNAVAILABLE_LIVE_RESULT,
          error,
          cancelled: isRpcAbort(error) || run.controller.signal.aborted,
        }
      } finally {
        clearTimeout(deadlineTimer)
        run.controller.signal.removeEventListener('abort', abortAttempt)
      }
      if (!isCurrent(run)) {
        return { ...result, authoritative: false, cancelled: true }
      }
      result = incident.terminal ?? result
      phase.result = result
      if (result.authoritative) {
        clearForegroundTimer()
        if (queuedReconciliation?.owner === run && queuedReconciliation.incident === incident
          && (queuedReconciliation.waitingForPredecessor || queuedReconciliation.needsFresh)) {
          // A gap observed after this read started still needs a fresh proof.
          // Do not end the incident or release readiness using the old result.
          livePhase.value = incident.degraded ? 'degraded' : 'connecting'
        } else {
          clearIncidentTimer()
          incidents.delete(incidentKey(run.key))
          noticeDismissed.value = false
          livePhase.value = 'ready'
          ownership.armRecovery()
          proofRevision.value++
        }
      } else {
        incident.degraded = true
        clearForegroundTimer()
        livePhase.value = 'degraded'
        if (!result.cancelled && (result.sessionMissing
          || result.error instanceof SessionReadFailure && !result.error.retryable)) {
          incident.terminal = result
        } else if (shouldRetrySessionPhase(result.error)) {
          const delay = Math.max(retryAfterMs(result.error),
            Math.min(15_000, 500 * 2 ** Math.min(incident.attempts++, 5)))
          incident.notBefore = Math.max(incident.notBefore, performance.now() + delay)
        }
        if (incidentResult(incident)) publishTerminal(run, incident.terminal!)
      }
      return result
    })().finally(() => {
      phase.running = false
      if (isCurrent(run)) liveAttemptCompletion.value++
    })
    return phase.promise
  }

  function createRun(key: string, includeHistory: boolean): ActiveBootstrap {
    syncProofOwner()
    const incident = incidentFor(key)
    hasProvenAuthority ||= Boolean(options.incidentScope?.())
    metadataRecovery?.pause(false)
    options.cancelHistory()
    options.cancelSubscription()
    const run = ownership.start(key, includeHistory, () => ({
      scope: options.incidentScope?.() ?? '',
      incident,
      lease: null as SessionReadLease | null,
      admission: Promise.resolve(),
      criticalRequestsQueued: Promise.resolve(),
      // Waiting for a server retry hint is admission, not a history attempt.
      // Keep its own short budget after admission, within the incident limit.
      history: historyRuntime(Math.min(incident.deadlineAt,
        Date.now() + Math.max(0, incident.notBefore - performance.now()) + SESSION_BOOTSTRAP_BUDGET_MS)),
      live: liveRuntime(incident.deadlineAt),
    }))
    active = run
    metadataRecovery?.activate()
    const open = () => {
      run.lease = options.sessionReadLifecycle.open({
        sessionKey: key,
        ...(metadataRecovery ? { beforeMetadataRead: metadataRecovery.beforeMetadataRead } : {}),
        ...(options.isProvisionalDraft?.() ? { provisionalDraft: true } : {}),
        includeInitialHistory: includeHistory,
      })
      return run.lease.criticalRequestsQueued
    }
    if (incident.notBefore <= performance.now()) run.criticalRequestsQueued = open()
    else {
      run.admission = (async () => {
        while (incident.notBefore > performance.now() && !incidentResult(incident)) {
          if (!await waitForLiveAdmission(run)) return
        }
        if (isCurrent(run) && !incidentResult(incident)) open()
      })()
      run.criticalRequestsQueued = run.admission.then(() => run.lease?.criticalRequestsQueued)
    }
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
      && isCurrent(active)
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

    const incident = incidentFor(key)
    const terminal = incidentResult(incident)
    if (terminal) {
      liveRecoveryTerminal.value = true
      livePhase.value = 'degraded'
      return {
        generation: ownership.generation,
        criticalRequestsQueued: Promise.resolve(),
        history: active?.key === key ? active.history.promise : Promise.resolve(EMPTY_HISTORY_RESULT),
        live: Promise.resolve(terminal),
      }
    }
    // Lease replacement and transport events are automatic recovery triggers,
    // not exemptions from an already accepted server retry restriction.
    if (active && isCurrent(active) && incident.notBefore > performance.now()) {
      return { ...publicRun(active), live: retryLive() }
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
      || !isCurrent(run)
      || !leaseIsCurrent(run)
    ) {
      return startSessionBootstrap({ includeHistory: true, force: true }).history
    }
    if (run.history.running) return run.history.promise
    resetHistoryPhaseForRetry(run)
    run.history.promise = runHistoryPhase(run, true)
    return run.history.promise
  }

  function retryLive(explicit = false): Promise<SessionSubscriptionOutcome> {
    const run = active
    if (!run || !isCurrent(run)) {
      if (explicit) {
        const incident = incidentFor(options.sessionKey.value)
        renewIncident(incident)
        return startSessionBootstrap({ includeHistory: false, force: true }).live
      }
      // There is no current session owner to schedule work for. A transport
      // notification must not create a new lease after page cancellation.
      return Promise.resolve({ ...UNAVAILABLE_LIVE_RESULT, cancelled: true })
    }
    if (explicit && queuedReconciliation?.owner === run
      && queuedReconciliation.incident === run.incident) return queuedReconciliation.promise
    if (explicit && run.live.running) return run.live.promise
    const incident = incidentFor(run.key)
    run.incident = incident
    if (explicit) renewIncident(incident)
    const terminal = incidentResult(incident)
    if (terminal) {
      publishTerminal(run, terminal)
      return Promise.resolve(terminal)
    }
    if (queuedReconciliation?.owner === run && queuedReconciliation.incident === incident) {
      if (run.live.running && !queuedReconciliation.waitingForPredecessor) {
        queuedReconciliation.needsFresh = true
        livePhase.value = incident.degraded ? 'degraded' : 'connecting'
      }
      return queuedReconciliation.promise
    }

    const request = {
      owner: run,
      incident,
      waitingForPredecessor: run.live.running,
      needsFresh: false,
      promise: Promise.resolve(UNAVAILABLE_LIVE_RESULT),
    }
    queuedReconciliation = request
    const work = (async () => {
      if (run.live.running) await run.live.promise
      request.waitingForPredecessor = false
      let owner = run
      for (;;) {
        if (!isCurrent(owner)) return { ...UNAVAILABLE_LIVE_RESULT, cancelled: true }
        // The preceding read can have failed with a new retry hint while this
        // request was waiting. All entry points join this one admission wait.
        while (incident.notBefore > performance.now() && !incidentResult(incident)) {
          if (!await waitForLiveAdmission(owner)) return { ...UNAVAILABLE_LIVE_RESULT, cancelled: true }
        }
        const blocked = incidentResult(incident)
        if (blocked) {
          publishTerminal(owner, blocked)
          return blocked
        }
        request.needsFresh = false
        const previousHistoryPhase = historyPhase.value
        const replace = !leaseIsCurrent(owner)
          || !options.reconcileSession
          || owner.live.result?.error instanceof SessionReadFailure
            && (owner.live.result.error.kind === 'rebase-required' || !owner.live.result.error.retryable)
        let result: SessionSubscriptionOutcome
        if (replace) {
          const includeHistory = previousHistoryPhase !== 'ready'
          const replacement = startSessionBootstrap({ includeHistory, force: true })
          owner = active!
          // Only this request's own replacement inherits its pending work.
          // An unrelated successor must not inherit the predecessor's flags.
          request.owner = owner
          if (!includeHistory) historyPhase.value = previousHistoryPhase
          result = await replacement.live
        } else {
          owner.live = liveRuntime(incident.deadlineAt)
          result = await runLivePhase(owner, true)
        }
        // An invalidation during this attempt cannot be proved by its older
        // snapshot. Coalesce all such signals into one subsequent read.
        if (!request.needsFresh || !result.authoritative) return result
      }
    })()
    request.promise = work.finally(() => {
      if (queuedReconciliation === request) {
        queuedReconciliation = null
        // A completed failed retry can leave the phase text unchanged. Wake
        // the watcher only after this admission is free for the next attempt.
        liveAttemptCompletion.value++
      }
    })
    // A synchronous ready observer can invalidate a read before its finally
    // clears running. Publish only after installing the real joinable promise.
    if (request.owner === active && (request.waitingForPredecessor || request.needsFresh)) {
      livePhase.value = incident.degraded ? 'degraded' : 'connecting'
    }
    return request.promise
  }

  /** Deliberate UI recovery; automatic live/metadata triggers never call this. */
  function retrySession(): Promise<boolean> {
    if (disposed || !options.sessionKey.value || !active || !isCurrent(active)) return Promise.resolve(false)
    if (sessionRetry?.isCurrent()) return sessionRetry.promise
    cancelSessionRetry()
    const checkpoint = recoveryProof.capture()
    const controller = new AbortController()
    const current = () => !controller.signal.aborted && checkpoint.isCurrent()
    const operation = { isCurrent: current, controller, promise: Promise.resolve(false) }
    operation.promise = Promise.resolve().then(async () => {
      if (!current()) return false
      const metadataNeeded = metadataRecovery && metadataRecovery.phase.value !== 'ready'
      // Admit metadata before repairing live: a replacement lease must inherit
      // this same bounded incident and its outstanding server retry hint.
      const metadata = metadataNeeded
        ? metadataRecovery.retryMetadata({ explicit: true, signal: controller.signal })
        : Promise.resolve(true)
      const live = active?.live.result && isCurrentLiveResult(active.live.result)
        ? Promise.resolve(active.live.result)
        : retryLive(true)
      const [metadataReady, liveResult] = await Promise.all([metadata, live])
      return current() && metadataReady && isCurrentLiveResult(liveResult)
    }).catch(() => false).finally(() => {
      if (sessionRetry === operation) {
        sessionRetry = null
        sessionRetryBusy.value = false
      }
    })
    // Publish the real joinable promise before reactive callbacks can re-enter.
    sessionRetry = operation
    sessionRetryBusy.value = true
    return operation.promise
  }

  function cancelSessionBootstrap(unsubscribe = true) {
    cancelSessionRetry()
    metadataRecovery?.pause()
    proofOwner = {}
    proofRevision.value++
    clearRecoveryTimer()
    clearForegroundTimer()
    clearIncidentTimer()
    recoveryAttempt = 0
    const cancelled = ownership.cancel() ?? active
    active = null
    queuedReconciliation = null
    options.cancelHistory()
    options.cancelSubscription()
    historyPhase.value = 'idle'
    livePhase.value = 'idle'
    liveRecoveryTerminal.value = false
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
      || !leaseIsCurrent(run)
    ) return
    return publicRun(run)
  }

  function isCurrentLiveResult(result: SessionSubscriptionOutcome): boolean {
    const run = active
    const invalidated = queuedReconciliation?.owner === run
      && queuedReconciliation?.incident === run?.incident
      && (queuedReconciliation?.waitingForPredecessor || queuedReconciliation?.needsFresh)
    return Boolean(result.authoritative && run && isCurrent(run) && leaseIsCurrent(run)
      && !invalidated && livePhase.value === 'ready' && run.live.result === result)
  }

  /** Join the current owner without giving consumers another retry scheduler. */
  async function waitForLiveReady(
    admission: { explicit?: boolean } = {},
  ): Promise<SessionLiveReadiness | false> {
    const sameOwner = recoveryProof.capture().isCurrent
    const ready: SessionLiveReadiness = () => Boolean(sameOwner()
      && active?.live.result && isCurrentLiveResult(active.live.result))
    if (ready()) return ready
    const run = active
    let pending = run && isCurrent(run) && queuedReconciliation?.owner === run
      && queuedReconciliation.incident === run.incident
      ? queuedReconciliation.promise
      : run && isCurrent(run) && run.live.running
        ? run.live.promise
        : retryLive(admission.explicit !== false)
    const joined = new Set<Promise<SessionSubscriptionOutcome>>()
    while (sameOwner() && !joined.has(pending)) {
      joined.add(pending)
      await pending
      if (!sameOwner()) return false
      if (ready()) return ready
      const run = active
      if (!run || !isCurrent(run)) return false
      // A gap can arrive after the caller joined its predecessor. Waiting on
      // that predecessor's result cannot authorize a command; follow work the
      // coordinator already owns without renewing its deadline or adding reads.
      if (queuedReconciliation?.owner === run
        && queuedReconciliation.incident === run.incident) pending = queuedReconciliation.promise
      else if (run.live.running) pending = run.live.promise
      else return false
    }
    return false
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
        && isCurrent(run)
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
    if (run && bindInitialAuthority(run) && leaseIsCurrent(run)) return publicRun(run)
    if (!run || !isCurrent(run)) {
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
    [options.sessionKey, options.connectionState, historyPhase, livePhase, liveAttemptCompletion,
      () => options.incidentScope?.() ?? ''],
    () => {
      clearRecoveryTimer()
      const run = active
      if (!run || options.connectionState?.value !== 'connected') return
      if (run.key === options.sessionKey.value && !run.controller.signal.aborted
        && run.scope !== (options.incidentScope?.() ?? '')) {
        if (!bindInitialAuthority(run)) {
          // Once any authority has been proven, losing or changing it needs
          // a fenced successor. Unknown is never a bridge between principals.
          startSessionBootstrap({ includeHistory: run.includeHistory, force: true })
          return
        }
      }
      if (!isCurrent(run)) return
      const historyFailed = historyPhase.value === 'error'
        && shouldRetrySessionPhase(run.history.result?.error)
      const liveFailed = (livePhase.value === 'degraded'
        && !run.live.running
        && !run.incident.terminal
        && !run.live.result?.sessionMissing
        && shouldRetrySessionPhase(run.live.result?.error))
      if (liveFailed && queuedReconciliation?.owner !== run) void retryLive().catch(() => {})
      if (!historyFailed && !liveFailed) {
        if (historyPhase.value === 'ready' && livePhase.value === 'ready') recoveryAttempt = 0
        return
      }
      if (!historyFailed) return
      const delayMs = Math.max(
        historyFailed ? retryAfterMs(run.history.result?.error) : 0,
        Math.min(15_000, 500 * 2 ** Math.min(recoveryAttempt, 5)),
      )
      recoveryTimer = setTimeout(() => {
        recoveryTimer = null
        if (!isCurrent(run) || options.connectionState?.value !== 'connected') return
        recoveryAttempt++
        if (historyFailed) void retryHistory().catch(() => {})
      }, delayMs)
    },
  ) : undefined
  if (getCurrentScope()) onScopeDispose(() => {
    cancelSessionRetry()
    metadataRecovery?.dispose()
    disposed = true
    proofOwner = {}
    stopProofWatch()
    stopRecoveryWatch?.()
    clearRecoveryTimer()
    clearForegroundTimer()
    clearIncidentTimer()
  })

  return {
    historyPhase,
    livePhase,
    liveRecoveryTerminal,
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
    retrySession,
    sessionRetryBusy,
    noticeDismissed,
    dismissRecoveryNotice,
    handleConnectionState,
    setSessionHandoffTarget,
    isSessionBootstrapCurrent,
    currentSessionBootstrap,
    isCurrentLiveResult,
    waitForLiveReady,
    recoveryProof,
    metadataRecovery,
    retryMetadata: (request?: import('./createSessionMetadataRecovery').MetadataRecoveryRequest) =>
      metadataRecovery?.retryMetadata(request) ?? Promise.resolve(false),
    canApplyMetadata: () => metadataRecovery?.canApplyMetadata() ?? true,
  }
}
