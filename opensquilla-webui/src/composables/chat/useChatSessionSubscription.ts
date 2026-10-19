import { ref, shallowReadonly, shallowRef, type Ref } from 'vue'
import type {
  ChatRunStatus,
  ChatRunStatusSource,
} from '@/types/chat'
import {
  SessionReadSessionMissingError,
  SessionReadFailure,
  SessionReadContractError,
  SessionReadLeaseClosedError,
  type SessionReadActivity,
  type SessionReadLease,
  type SessionReadLeaseReader,
  type SessionReadMetadata,
  type SessionReadRunModeLock,
  type SessionReadSnapshot,
} from '@/modules/sessionReadLifecycle'
import type { ConversationCursorSignal, ConversationRuntime } from '@/modules/conversationRuntime'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import { chatTaskId } from '@/composables/chat/useChatTaskOwnership'
import {
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  type SessionBootstrapPhaseContext,
  type SessionPhaseResult,
} from '@/composables/chat/sessionBootstrapContract'

export interface UseChatSessionSubscriptionOptions {
  /** The bootstrap owner supplies the one lease shared by live and history consumers. */
  sessionReadLeaseReader: SessionReadLeaseReader
  /** Shared domain cursor policy used by the existing live-event projection. */
  conversationRuntime: ConversationRuntime
  sessionKey: Ref<string>
  lastStreamSeq: Ref<number>
  runStatus: Ref<ChatRunStatus>
  isStreaming: Ref<boolean>
  hasActiveInterrupt: Ref<boolean>
  activeStreamTaskId: Ref<string>
  activeTaskGroups: Ref<Set<string>>
  taskOwnership?: ChatTaskOwnershipApi
  ownershipHydrationRequired?: () => boolean
  /** The coordinator checks the monotonic incident deadline at application time. */
  metadataApplicationAllowed?: () => boolean
  acceptanceStopPending?: Ref<boolean>
  sessionRunStatus: (source: ChatRunStatusSource | null | undefined) => ChatRunStatus
  startStreaming: (
    activityStartedAt?: number | string | null,
    recordInitialActivity?: boolean,
  ) => void
  /** Adopt durable task timing even when snapshot replay already opened the bubble. */
  reconcileStreamTaskClock?: (snapshot: {
    sessionKey: string
    taskId: string
    startedAt?: number | string | null
  }) => boolean | void
  loadHistory: () => void | Promise<SessionPhaseResult | void>
  reconcileHistory?: () => Promise<SessionPhaseResult | void>
  resetStreamIdleTimer: () => void
  resetStreamLiveTurnState: () => void
  /** Retire maintenance owned by a replaced process before replaying its successor. */
  onStreamGenerationReset?: () => void
  onLiveSnapshot?: (snapshot: SessionReadSnapshot) => void
  onReadStarted?: () => void
  onSnapshotInstalled?: () => void
  onReconciliationInstalled?: () => Promise<void>
  onAuthoritativeIdle?: () => void
  onRunModeLock?: (lock: SessionReadRunModeLock) => void
  beginSessionMetadataResolution?: (key: string) => number
  onSessionMetadata?: (
    key: string,
    generation: number,
    metadata: SessionReadMetadata,
  ) => void
  onSessionMetadataError?: (key: string, generation: number) => void
  onSessionMissing?: (key: string) => void
  onSnapshot?: (snapshot: SessionReadMetadata) => void
}

export interface SessionMetadataRetryOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

export type SessionMetadataFailure = SessionReadFailure | SessionReadContractError | SessionReadSessionMissingError

export interface SessionMetadataOutcome {
  readonly ok: boolean
  readonly error?: SessionMetadataFailure
  readonly cancelled?: boolean
  readonly sessionMissing?: boolean
}

export interface SessionMetadataState {
  readonly phase: 'idle' | 'loading' | 'ready' | 'error'
  readonly key: string
  readonly lease: SessionReadLease | null
  readonly attempt: number
  readonly outcome: SessionMetadataOutcome | null
}

interface MetadataAttempt {
  key: string
  lease: SessionReadLease
  sequence: number
  attempt: number
  controller: AbortController
  promise: Promise<SessionMetadataOutcome>
  subscription: SessionSubscriptionOutcome | null
}

const LIVE_RUN_STATES = ['queued', 'running', 'approval_pending']

export interface SessionSubscriptionOutcome {
  authoritative: boolean
  live: boolean
  backgroundOnly: boolean
  error?: unknown
  cancelled?: boolean
  skipSnapshotOnRetry?: boolean
  /** Terminal domain state: the requested session no longer exists. */
  sessionMissing?: boolean
}

export type SessionSubscriptionResult = boolean | void | SessionSubscriptionOutcome

/** Treat only explicit structured failures (or legacy false) as non-authoritative. */
export function isAuthoritativeSessionSubscription(
  value: SessionSubscriptionResult,
): boolean {
  if (typeof value === 'object' && value !== null) return value.authoritative === true
  return value !== false
}

const UNAVAILABLE_SUBSCRIPTION: SessionSubscriptionOutcome = {
  authoritative: false,
  live: false,
  backgroundOnly: false,
}

function localAbortError(message: string): Error {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

function waitForMetadataRetry<T>(
  operation: Promise<T>,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<T> {
  if (signal.aborted) {
    // The operation may synchronously revoke its owner before returning its
    // promise. Observe that physical failure even when the caller has left.
    void operation.catch(() => {})
    return Promise.reject(localAbortError('Metadata retry was cancelled.'))
  }
  return new Promise<T>((resolve, reject) => {
    let settled = false
    const finish = (callback: () => void) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      callback()
    }
    const abort = () => finish(() => reject(localAbortError('Metadata retry was cancelled.')))
    const timer = setTimeout(
      () => finish(() => reject(new SessionReadFailure('timeout', 'Session metadata recovery timed out.', true))),
      timeoutMs,
    )
    signal.addEventListener('abort', abort, { once: true })
    operation.then(
      value => finish(() => resolve(value)),
      error => finish(() => reject(error)),
    )
  })
}

function metadataFailure(cause: unknown): SessionMetadataFailure {
  if (cause instanceof SessionReadFailure || cause instanceof SessionReadContractError
    || cause instanceof SessionReadSessionMissingError) return cause
  if (cause instanceof SessionReadLeaseClosedError || cause instanceof Error && cause.name === 'AbortError') {
    return new SessionReadFailure('aborted', 'Session metadata owner was cancelled.', false, 0, cause)
  }
  // Transport/domain adapters supply typed failures. An unexpected operation
  // failure remains bounded by the coordinator, without parsing message text.
  return new SessionReadFailure('unavailable', 'Session metadata could not be read.', true, 0, cause)
}

function metadataFailureOutcome(cause: unknown): SessionMetadataOutcome {
  const error = metadataFailure(cause)
  return {
    ok: false, error,
    cancelled: error instanceof SessionReadFailure && error.kind === 'aborted',
    sessionMissing: error instanceof SessionReadSessionMissingError,
  }
}

export function useChatSessionSubscription(options: UseChatSessionSubscriptionOptions) {
  const isHydrating = ref(false)
  const metadataRecoveryError = ref<unknown>(null)
  const metadataState = shallowRef<SessionMetadataState>({
    phase: 'idle', key: options.sessionKey.value, lease: null, attempt: 0, outcome: null,
  })
  const streamGeneration = ref<string | null>(null)
  const conversationRuntime = options.conversationRuntime
  let activeSubscriptionController: AbortController | null = null
  let subscriptionSequence = 0
  let activeMetadataAttempt: MetadataAttempt | null = null
  let metadataHydrationSequence = 0

  function cursor() {
    return conversationRuntime.createCursor(options.sessionKey.value, {
      streamGeneration: streamGeneration.value,
      streamSeq: options.lastStreamSeq.value,
    })
  }

  function syncCursor(next: ReturnType<ConversationRuntime['createCursor']>) {
    streamGeneration.value = next.streamGeneration
    options.lastStreamSeq.value = next.streamSeq
  }

  function subscribeSession(
    bootstrap?: SessionBootstrapPhaseContext,
    reconciliation = false,
  ): Promise<SessionSubscriptionOutcome> {
    if (!options.sessionKey.value) return Promise.resolve(UNAVAILABLE_SUBSCRIPTION)
    if (options.ownershipHydrationRequired?.() !== false) {
      options.taskOwnership?.beginHydration()
    }
    const key = options.sessionKey.value
    const lease = options.sessionReadLeaseReader.current()
    if (!lease) return Promise.resolve(UNAVAILABLE_SUBSCRIPTION)
    const sequence = ++subscriptionSequence
    cancelMetadataAttempt()
    activeSubscriptionController?.abort()
    const controller = new AbortController()
    activeSubscriptionController = controller
    const relayAbort = () => controller.abort()
    if (bootstrap?.signal.aborted) controller.abort()
    else bootstrap?.signal.addEventListener('abort', relayAbort, { once: true })
    return runSubscription(lease, key, sequence, controller.signal, bootstrap, reconciliation)
      .finally(() => {
        bootstrap?.signal.removeEventListener('abort', relayAbort)
        if (activeSubscriptionController === controller) {
          activeSubscriptionController = null
        }
      })
  }

  /**
   * Observe a generation-bearing live event before applying its numeric cursor.
   * The event-handler integration calls this first so a restarted Gateway's low
   * sequence numbers are accepted instead of compared with the retired stream.
   */
  function observeStreamGeneration(signal: ConversationCursorSignal): boolean {
    const transition = conversationRuntime.observeGeneration(
      cursor(),
      signal,
    )
    if (!transition.changed) return false
    syncCursor(transition.cursor)
    if (!transition.reset) return false
    options.resetStreamLiveTurnState()
    return true
  }

  function applyHydratedSubscriptionState(
    key: string,
    metadataGeneration: number | undefined,
    metadata: SessionReadMetadata,
    activity: SessionReadActivity = 'unknown',
    ownershipRevision?: number,
    isCurrent?: () => boolean,
  ): SessionSubscriptionOutcome {
    const assertCurrent = () => {
      if (isCurrent && !isCurrent()) throw localAbortError('Session metadata owner changed during installation.')
      if (options.metadataApplicationAllowed?.() === false) {
        throw new SessionReadFailure('budget-exhausted', 'Session metadata recovery deadline elapsed.', false)
      }
    }
    assertCurrent()
    if (metadata.sessionKey !== key || !metadata.hydrationComplete) {
      // The hydrate contract requires hydration_complete: true. Deferred
      // project facets are allowed, but a partial hydrate response is invalid.
      throw new SessionReadContractError('Session metadata hydration returned an incomplete or mismatched result.')
    }
    assertOwnershipSnapshotCurrent(ownershipRevision)
    if (metadataGeneration !== undefined) {
      options.onSessionMetadata?.(key, metadataGeneration, metadata)
    }
    assertCurrent()
    options.onRunModeLock?.(metadata.runModeLock)
    assertCurrent()
    const source = metadataRunStatusSource(metadata)
    const rawActiveTask = source.activeTask || null
    const rawActiveTaskId = chatTaskId(rawActiveTask)
    const rawRunStatus = metadata.runStatus.toLowerCase()
    const settledLiveTask = LIVE_RUN_STATES.includes(rawRunStatus)
      && Boolean(rawActiveTaskId)
      && options.taskOwnership?.isSettled(rawActiveTaskId) === true
    const effectiveMetadata = settledLiveTask
      ? { ...metadata, runStatus: 'idle', activeTask: null }
      : metadata
    const effectiveSource = metadataRunStatusSource(effectiveMetadata)
    options.onSnapshot?.(effectiveMetadata)
    assertCurrent()
    assertOwnershipSnapshotCurrent(ownershipRevision)
    if (options.taskOwnership && !options.taskOwnership.applySnapshot(effectiveSource, true, ownershipRevision)) {
      throw new SessionReadFailure('busy', 'Task ownership changed before session metadata was installed.', true)
    }
    assertCurrent()
    // Do not clear an acceptance-result-unknown Stop from an idle snapshot.
    // The subscription can race ahead of the original ingress commit, so only
    // the matching send transaction (receipt/rejection) or an explicit session
    // reset may release that latch.  Its idempotent replay must still inherit
    // the Stop intent and abort the exact accepted task once the receipt exists.
    applySessionRunState(effectiveSource)
    // Preserve an existing execution interrupted by a request that raced an
    // idle snapshot. A standalone/recovered approval is not itself evidence
    // of execution and must not manufacture a running task or elapsed clock.
    if (
      options.hasActiveInterrupt.value
      && options.isStreaming.value
      && !LIVE_RUN_STATES.includes(options.runStatus.value.status)
    ) {
      options.runStatus.value = options.sessionRunStatus({
        run_status: 'approval_pending',
        active_task: options.runStatus.value.task,
      })
    }
    const liveTaskSnapshot = LIVE_RUN_STATES.includes(options.runStatus.value.status)
    if (!settledLiveTask) reconcileActiveTaskGroups(metadata)
    if (liveTaskSnapshot && !options.isStreaming.value) {
      const activeTask = effectiveMetadata.activeTask as {
        started_at?: number | string | null
        startedAt?: number | string | null
      } | null | undefined
      options.startStreaming(activeTask?.started_at ?? activeTask?.startedAt)
      // startStreaming establishes the live bubble with a generic running
      // placeholder. Restore the authoritative active-task payload (including
      // steer_capability) that came from hydration instead of waiting for a
      // later task.running event to repair it.
      applySessionRunState(effectiveSource)
    }
    if (liveTaskSnapshot) {
      const activeTask = effectiveMetadata.activeTask as {
        task_id?: string
        taskId?: string
        started_at?: number | string | null
        startedAt?: number | string | null
      } | null | undefined
      const taskId = activeTask?.task_id || activeTask?.taskId
      if (taskId) {
        options.activeStreamTaskId.value = taskId
        if (options.runStatus.value.status !== 'queued') {
          options.reconcileStreamTaskClock?.({
            sessionKey: key,
            taskId,
            startedAt: activeTask?.started_at ?? activeTask?.startedAt,
          })
        }
      }
    }
    // Replayed events can rebuild a live bubble for work that is already
    // terminal. An authoritative idle snapshot removes only that stale tail.
    if (
      options.isStreaming.value
      && !options.hasActiveInterrupt.value
      && !liveTaskSnapshot
    ) {
      options.resetStreamLiveTurnState()
    }
    if (options.isStreaming.value) options.resetStreamIdleTimer()
    const taskOrInterruptLive = (
      liveTaskSnapshot
      || options.hasActiveInterrupt.value
      || (activity === 'foreground' && !settledLiveTask)
    )
    const groupLive = options.activeTaskGroups.value.size > 0 || activity === 'background'
    const outcome = {
      authoritative: true,
      live: taskOrInterruptLive || groupLive,
      backgroundOnly: groupLive && !taskOrInterruptLive,
    }
    if (!outcome.live) options.onAuthoritativeIdle?.()
    assertCurrent()
    metadataRecoveryError.value = null
    return outcome
  }

  function metadataRunStatusSource(metadata: SessionReadMetadata): ChatRunStatusSource {
    return {
      runStatus: metadata.runStatus,
      activeTask: metadata.activeTask,
      lastTask: metadata.lastTask,
      tasks: metadata.tasks,
      queuedTaskIds: metadata.queuedTaskIds,
    } as unknown as ChatRunStatusSource
  }

  function isCurrentSubscription(
    lease: SessionReadLease,
    key: string,
    sequence: number,
    signal?: AbortSignal,
  ): boolean {
    return sequence === subscriptionSequence
      && key === options.sessionKey.value
      && options.sessionReadLeaseReader.current() === lease
      && signal?.aborted !== true
  }

  function assertOwnershipSnapshotCurrent(revision: number | undefined) {
    if (
      revision !== undefined
      && options.taskOwnership?.captureSnapshotRevision() !== revision
    ) {
      throw new SessionReadFailure('busy', 'Task ownership changed while reading session metadata.', true)
    }
  }

  function isCurrentMetadataAttempt(attempt: MetadataAttempt): boolean {
    return activeMetadataAttempt === attempt
      && attempt.attempt === metadataHydrationSequence
      && isCurrentSubscription(attempt.lease, attempt.key, attempt.sequence, attempt.controller.signal)
  }

  function cancelMetadataAttempt() {
    const attempt = activeMetadataAttempt
    if (!attempt) return
    activeMetadataAttempt = null
    attempt.controller.abort()
    if (metadataState.value.attempt === attempt.attempt) {
      metadataState.value = {
        ...metadataState.value,
        phase: 'error',
        outcome: metadataFailureOutcome(localAbortError('Session metadata attempt was cancelled.')),
      }
    }
  }

  /** One bounded operation. The bootstrap owner, not this adapter, retries it. */
  function startMetadataAttempt(
    lease: SessionReadLease,
    key: string,
    sequence: number,
    metadataGeneration: number | undefined,
    activity: SessionReadActivity,
    signal: AbortSignal | undefined,
    ownershipRevision: number | undefined,
    operation: () => Promise<SessionReadMetadata>,
    timeoutMs = SESSION_PHASE_ATTEMPT_BUDGET_MS,
  ): MetadataAttempt {
    cancelMetadataAttempt()
    const controller = new AbortController()
    const relayAbort = () => controller.abort()
    if (signal?.aborted) controller.abort()
    else signal?.addEventListener('abort', relayAbort, { once: true })
    let complete!: (outcome: SessionMetadataOutcome) => void
    const attempt: MetadataAttempt = {
      key, lease, sequence, attempt: ++metadataHydrationSequence, controller,
      promise: new Promise(resolve => { complete = resolve }), subscription: null,
    }
    activeMetadataAttempt = attempt
    metadataState.value = { phase: 'loading', key, lease, attempt: attempt.attempt, outcome: null }
    // A synchronous owner watcher can reject this attempt before it starts,
    // for example when the retained incident deadline has already elapsed.
    void (async (): Promise<SessionMetadataOutcome> => {
      try {
        if (!isCurrentMetadataAttempt(attempt)) return metadataFailureOutcome(localAbortError('Session metadata owner changed.'))
        const metadata = await waitForMetadataRetry(operation(), controller.signal, Math.max(1, timeoutMs))
        if (!isCurrentMetadataAttempt(attempt)) return metadataFailureOutcome(localAbortError('Session metadata owner changed.'))
        attempt.subscription = applyHydratedSubscriptionState(key, metadataGeneration, metadata, activity,
          ownershipRevision, () => isCurrentMetadataAttempt(attempt))
        if (!isCurrentMetadataAttempt(attempt)) return metadataFailureOutcome(localAbortError('Session metadata owner changed during installation.'))
        const outcome: SessionMetadataOutcome = { ok: true }
        metadataState.value = { phase: 'ready', key, lease, attempt: attempt.attempt, outcome }
        return outcome
      } catch (cause) {
        if (!isCurrentMetadataAttempt(attempt)) return metadataFailureOutcome(localAbortError('Session metadata owner changed.'))
        const outcome = metadataFailureOutcome(cause)
        if (isCurrentMetadataAttempt(attempt)) {
          metadataRecoveryError.value = outcome.error
          if (metadataGeneration !== undefined) options.onSessionMetadataError?.(key, metadataGeneration)
          if (outcome.sessionMissing) options.onSessionMissing?.(key)
          metadataState.value = { phase: 'error', key, lease, attempt: attempt.attempt, outcome }
          if (!outcome.cancelled) console.warn('Session metadata hydration failed:', outcome.error?.message)
        }
        return outcome
      } finally {
        signal?.removeEventListener('abort', relayAbort)
        if (activeMetadataAttempt === attempt) activeMetadataAttempt = null
      }
    })().then(complete)
    return attempt
  }

  async function runSubscription(
    lease: SessionReadLease,
    key: string,
    sequence: number,
    signal: AbortSignal,
    bootstrap?: SessionBootstrapPhaseContext,
    reconciliation = false,
  ): Promise<SessionSubscriptionOutcome> {
    let ownershipRevision = options.taskOwnership?.captureSnapshotRevision()
    const metadataGeneration = options.beginSessionMetadataResolution?.(key)
    if (options.lastStreamSeq.value === 0) isHydrating.value = true
    try {
      if (signal.aborted || !isCurrentSubscription(lease, key, sequence, signal)) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      options.onReadStarted?.()
      const live = await (reconciliation ? lease.reconcile() : lease.live)
      if (!isCurrentSubscription(lease, key, sequence, signal)) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      assertOwnershipSnapshotCurrent(ownershipRevision)
      async function finishInstallation() {
        await live.confirmInstalled?.()
        if (!isCurrentSubscription(lease, key, sequence, signal)) throw localAbortError('Snapshot owner changed.')
        options.onSnapshotInstalled?.()
        // Projection/tail application can itself detect loss. Recheck the
        // adapter's exact invalidation fence after that final consumer step.
        live.assertInstalledCurrent?.()
        if (!isCurrentSubscription(lease, key, sequence, signal)) throw localAbortError('Snapshot owner changed.')
      }
      if (live.reloadRequired === 'generationChanged') {
        // The replacement can be idle and have no event carrying its new
        // generation. Explicitly retire old owners before installing any new
        // snapshot; otherwise its maintenance would remain busy indefinitely.
        options.onStreamGenerationReset?.()
        syncCursor(conversationRuntime.reset(cursor()))
        options.resetStreamLiveTurnState()
      }
      let snapshotTaskLive = false
      const snapshot = live.snapshot
      if (snapshot?.sessionKey === key) {
        const snapshotTaskId = snapshot.taskId || ''
        const settledSnapshot = Boolean(
          snapshotTaskId && options.taskOwnership?.isSettled(snapshotTaskId),
        )
        if (!settledSnapshot) options.onLiveSnapshot?.(snapshot)
        snapshotTaskLive = Boolean(snapshotTaskId) && !settledSnapshot
      }
      // Applying this read's own live snapshot can legitimately claim its task.
      // Only changes arriving after that installation invalidate the metadata.
      ownershipRevision = options.taskOwnership?.captureSnapshotRevision()
      if (live.reloadRequired) {
        if (!reconciliation) void options.loadHistory()
      }
      // A live snapshot cannot recover a terminal answer whose streaming
      // projection has already been cleared. Refresh durable history on every
      // explicit reconciliation, retaining the displayed window while it loads.
      if (reconciliation) {
        const history = await (options.reconcileHistory?.() ?? options.loadHistory())
        if (history && !history.ok) {
          throw history.error ?? new SessionReadFailure('unavailable', 'History reconciliation is incomplete.', true)
        }
      }
      if (!isCurrentSubscription(lease, key, sequence, signal)) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      if (live.initialMetadata.hydrationComplete) {
        const attempt = startMetadataAttempt(lease, key, sequence, metadataGeneration, live.activity,
          signal, ownershipRevision, () => Promise.resolve(live.initialMetadata))
        const metadataOutcome = await attempt.promise
        if (metadataOutcome.ok) {
          if (reconciliation) await options.onReconciliationInstalled?.()
          if (!isCurrentSubscription(lease, key, sequence, signal)) return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
          await finishInstallation()
          return attempt.subscription!
        }
        // Metadata can be terminal while delivery registration remains sound.
        // Keep task ownership unresolved; only an actual subscription change
        // cancels live installation. The coordinator owns metadata recovery.
        if (!isCurrentSubscription(lease, key, sequence, signal)) return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      } else {
        if (options.ownershipHydrationRequired?.() !== false) {
          options.taskOwnership?.applySnapshot(
            metadataRunStatusSource(live.initialMetadata),
            false,
          )
        }
        startMetadataAttempt(
          lease,
          key,
          sequence,
          metadataGeneration,
          live.activity,
          signal,
          ownershipRevision,
          () => reconciliation ? lease.retryMetadata({ fresh: true }) : lease.metadata,
        )
      }
      // Fast ACK is authoritative for delivery registration. Deferred storage
      // metadata may refine task/workspace state later but cannot make history
      // or the real-time channel non-terminal.
      const taskOrInterruptLive = (
        snapshotTaskLive
        || options.isStreaming.value
        || options.hasActiveInterrupt.value
        || live.activity === 'foreground'
      )
      await finishInstallation()
      return {
        authoritative: true,
        live: taskOrInterruptLive || live.activity === 'background',
        backgroundOnly: live.activity === 'background' && !taskOrInterruptLive,
      }
    } catch (err: unknown) {
      const cancelled = (
        !isCurrentSubscription(lease, key, sequence)
        || signal.aborted
        || isRpcAbort(err)
      )
      // Replacing a socket/lease intentionally aborts its pending read. Keep
      // genuine current failures observable without reporting cancellation as one.
      if (!cancelled) console.warn('Session stream subscription failed:', err instanceof Error ? err.message : err)
      const sessionMissing = !cancelled
        && err instanceof SessionReadSessionMissingError
      if (sessionMissing) options.onSessionMissing?.(key)
      if (
        metadataGeneration !== undefined
        && !cancelled
        && (!bootstrap || bootstrap.attempt === 1)
        && isCurrentSubscription(lease, key, sequence)
        && key === options.sessionKey.value
      ) {
        options.onSessionMetadataError?.(key, metadataGeneration)
      }
      return {
        ...UNAVAILABLE_SUBSCRIPTION,
        error: err,
        cancelled,
        sessionMissing,
      }
    } finally {
      if (isCurrentSubscription(lease, key, sequence)) isHydrating.value = false
    }
  }

  function runMetadataAttempt(
    retryOptions: SessionMetadataRetryOptions = {},
  ): Promise<SessionMetadataOutcome> {
    if (retryOptions.signal?.aborted) return Promise.resolve(metadataFailureOutcome(localAbortError('Metadata caller was cancelled.')))
    const key = options.sessionKey.value
    const lease = options.sessionReadLeaseReader.current()
    if (!key || !lease) return Promise.resolve(metadataFailureOutcome(localAbortError('Session metadata has no current read owner.')))
    let attempt = activeMetadataAttempt
    if (!attempt || !isCurrentMetadataAttempt(attempt)) {
      attempt = startMetadataAttempt(lease, key, subscriptionSequence,
        options.beginSessionMetadataResolution?.(key), 'unknown', undefined,
        options.taskOwnership?.captureSnapshotRevision(), () => lease.retryMetadata({ fresh: true }),
        retryOptions.timeoutMs)
    }
    // Callers share the operation; a short caller timeout or cancellation only
    // stops that wait. The coordinator explicitly cancels the owned attempt.
    if (!retryOptions.signal && retryOptions.timeoutMs === undefined) return attempt.promise
    return waitForMetadataRetry(attempt.promise, retryOptions.signal ?? new AbortController().signal,
      Math.max(1, retryOptions.timeoutMs ?? SESSION_PHASE_ATTEMPT_BUDGET_MS))
      .catch(metadataFailureOutcome)
  }

  async function retrySessionMetadata(retryOptions: SessionMetadataRetryOptions = {}): Promise<boolean> {
    return (await runMetadataAttempt(retryOptions)).ok
  }

  function cancelActiveSubscription() {
    ++subscriptionSequence
    cancelMetadataAttempt()
    activeSubscriptionController?.abort()
    activeSubscriptionController = null
    metadataRecoveryError.value = null
    metadataState.value = {
      phase: 'idle', key: options.sessionKey.value, lease: null,
      attempt: metadataHydrationSequence, outcome: null,
    }
    isHydrating.value = false
  }

  async function unsubscribeSession(key = options.sessionKey.value) {
    cancelActiveSubscription()
    void key
  }

  function applySessionRunState(source: ChatRunStatusSource | null | undefined) {
    const next = options.sessionRunStatus(source)
    const current = options.runStatus.value
    const currentTaskId = chatTaskId(current.task)
    const nextTaskId = chatTaskId(next.task)
    if (
      !LIVE_RUN_STATES.includes(next.status)
      && !nextTaskId
      && options.taskOwnership?.stopTargetTaskId.value
    ) {
      // A compact, unscoped terminal summary cannot settle a known running
      // or queued task. A complete read first reconciles ownership via
      // applySnapshot, so genuine idle hydration still reaches this setter.
      return
    }
    if (next.status === 'queued' && nextTaskId) {
      options.taskOwnership?.noteQueued(next.task || nextTaskId)
      const runningTaskId = options.taskOwnership?.runningTaskId.value || ''
      // A compact task.queued or an older sessions.changed payload can name
      // the task that changed rather than the session foreground. Never let it
      // demote a different task that is already known to be running.
      if (runningTaskId && runningTaskId !== nextTaskId) return
    } else if (next.status === 'running' && nextTaskId) {
      options.taskOwnership?.noteRunning(next.task || nextTaskId)
    } else if (
      ['cancelled', 'failed', 'timeout', 'interrupted', 'idle'].includes(next.status)
      && nextTaskId
    ) {
      const settled = options.taskOwnership?.noteTerminal(nextTaskId)
      if (settled?.wasQueued && !settled.wasRunning && currentTaskId !== nextTaskId) return
      const runningTaskId = options.taskOwnership?.runningTaskId.value || ''
      if (runningTaskId && runningTaskId !== nextTaskId) return
    }
    if (
      LIVE_RUN_STATES.includes(current.status)
      && LIVE_RUN_STATES.includes(next.status)
      && current.task
      && next.task
    ) {
      if (currentTaskId && (!nextTaskId || nextTaskId === currentTaskId)) {
        // Lifecycle broadcasts are intentionally compact and can follow the
        // richer task.running frame for the same task. Preserve authoritative
        // fields such as steer_capability when the compact frame omits them.
        next.task = { ...current.task, ...next.task }
      }
    }
    options.runStatus.value = next
  }

  function reconcileActiveTaskGroups(metadata: SessionReadMetadata) {
    options.activeTaskGroups.value = new Set(metadata.activeTaskGroupIds.filter(Boolean))
    if (options.activeTaskGroups.value.size === 0) return
    applySessionRunState({
      run_status: 'running',
      active_task: {
        status: 'running',
        task_group_count: options.activeTaskGroups.value.size,
      },
    })
  }

  return {
    isHydrating,
    metadataRecoveryError,
    metadataState: shallowReadonly(metadataState),
    streamGeneration,
    observeStreamGeneration,
    subscribeSession,
    reconcileSession: (bootstrap?: SessionBootstrapPhaseContext) => subscribeSession(bootstrap, true),
    retrySessionMetadata,
    runMetadataAttempt,
    cancelMetadataAttempt,
    unsubscribeSession,
    cancelActiveSubscription,
    applySessionRunState,
  }
}
