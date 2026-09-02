import { ref, type Ref } from 'vue'
import type {
  ChatRunStatus,
  ChatRunStatusSource,
} from '@/types/chat'
import {
  SessionReadSessionMissingError,
  type SessionReadActivity,
  type SessionReadLease,
  type SessionReadLeaseReader,
  type SessionReadMetadata,
  type SessionReadRunModeLock,
  type SessionReadSnapshot,
} from '@/modules/sessionReadLifecycle'
import type { ConversationRuntime } from '@/modules/conversationRuntime'
import { conversationCursorSignal } from '@/utils/chat/streamEvents'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import { chatTaskId } from '@/composables/chat/useChatTaskOwnership'
import {
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  type SessionBootstrapPhaseContext,
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
  loadHistory: () => void | Promise<unknown>
  resetStreamIdleTimer: () => void
  resetStreamLiveTurnState: () => void
  onLiveSnapshot?: (snapshot: SessionReadSnapshot) => void
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
  onSnapshot?: (
    snapshot: SessionReadMetadata,
    streamGeneration: string | null,
  ) => void
}

export interface SessionMetadataRetryOptions {
  signal?: AbortSignal
  timeoutMs?: number
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
  if (signal.aborted) return Promise.reject(localAbortError('Metadata retry was cancelled.'))
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
      () => finish(() => reject(new Error('Session metadata recovery timed out.'))),
      timeoutMs,
    )
    signal.addEventListener('abort', abort, { once: true })
    operation.then(
      value => finish(() => resolve(value)),
      error => finish(() => reject(error)),
    )
  })
}

export function useChatSessionSubscription(options: UseChatSessionSubscriptionOptions) {
  const isHydrating = ref(false)
  const streamGeneration = ref<string | null>(null)
  const conversationRuntime = options.conversationRuntime
  let activeSubscriptionController: AbortController | null = null
  let subscriptionSequence = 0
  let activeMetadataController: AbortController | null = null
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
  ): Promise<SessionSubscriptionOutcome> {
    if (!options.sessionKey.value) return Promise.resolve(UNAVAILABLE_SUBSCRIPTION)
    if (options.ownershipHydrationRequired?.() !== false) {
      options.taskOwnership?.beginHydration()
    }
    const key = options.sessionKey.value
    const lease = options.sessionReadLeaseReader.current()
    if (!lease) return Promise.resolve(UNAVAILABLE_SUBSCRIPTION)
    const sequence = ++subscriptionSequence
    activeSubscriptionController?.abort()
    const controller = new AbortController()
    activeSubscriptionController = controller
    const relayAbort = () => controller.abort()
    if (bootstrap?.signal.aborted) controller.abort()
    else bootstrap?.signal.addEventListener('abort', relayAbort, { once: true })
    return runSubscription(lease, key, sequence, controller.signal, bootstrap)
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
  function observeStreamGeneration(source: unknown): boolean {
    const transition = conversationRuntime.observeGeneration(
      cursor(),
      conversationCursorSignal(source),
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
    snapshotStreamGeneration: string | null = null,
  ): SessionSubscriptionOutcome {
    if (metadataGeneration !== undefined) {
      options.onSessionMetadata?.(key, metadataGeneration, metadata)
    }
    options.onRunModeLock?.(metadata.runModeLock)
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
    options.onSnapshot?.(effectiveMetadata, snapshotStreamGeneration)
    options.taskOwnership?.applySnapshot(effectiveSource, true)
    // Do not clear an acceptance-result-unknown Stop from an idle snapshot.
    // The subscription can race ahead of the original ingress commit, so only
    // the matching send transaction (receipt/rejection) or an explicit session
    // reset may release that latch.  Its idempotent replay must still inherit
    // the Stop intent and abort the exact accepted task once the receipt exists.
    applySessionRunState(effectiveSource)
    // A pending inline interrupt is newer, stronger evidence than an idle
    // subscription snapshot that raced with the approval request.
    if (
      options.hasActiveInterrupt.value
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

  function scheduleMetadataHydration(
    lease: SessionReadLease,
    key: string,
    sequence: number,
    metadataHydration: number,
    metadataGeneration: number | undefined,
    activity: SessionReadActivity,
    snapshotStreamGeneration: string | null,
    signal: AbortSignal,
  ): void {
    void lease.metadata.then((metadata) => {
      if (
        !isCurrentSubscription(lease, key, sequence, signal)
        || metadataHydration !== metadataHydrationSequence
      ) return
      if (!metadata.hydrationComplete) {
        throw new Error('Session state hydration remained incomplete')
      }
      applyHydratedSubscriptionState(
        key,
        metadataGeneration,
        metadata,
        activity,
        snapshotStreamGeneration,
      )
    }).catch((cause) => {
      if (
        !isCurrentSubscription(lease, key, sequence, signal)
        || metadataHydration !== metadataHydrationSequence
      ) return
      if (metadataGeneration !== undefined) {
        options.onSessionMetadataError?.(key, metadataGeneration)
      }
      console.warn(
        'Session metadata hydration failed:',
        cause instanceof Error ? cause.message : cause,
      )
    })
  }

  async function runSubscription(
    lease: SessionReadLease,
    key: string,
    sequence: number,
    signal: AbortSignal,
    bootstrap?: SessionBootstrapPhaseContext,
  ): Promise<SessionSubscriptionOutcome> {
    const metadataHydration = ++metadataHydrationSequence
    const metadataGeneration = options.beginSessionMetadataResolution?.(key)
    if (options.lastStreamSeq.value === 0) isHydrating.value = true
    try {
      if (signal.aborted || !isCurrentSubscription(lease, key, sequence, signal)) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      const live = await lease.live
      if (!isCurrentSubscription(lease, key, sequence, signal)) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      if (live.streamGeneration) {
        observeStreamGeneration({
          sessionKey: key,
          streamGeneration: live.streamGeneration,
          ...(live.reloadRequired === 'generationChanged'
            ? { replayGapReason: 'stream_generation_changed' }
            : {}),
        })
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
      if (live.reloadRequired) {
        if (live.reloadRequired === 'generationChanged' && !live.streamGeneration) {
          syncCursor(conversationRuntime.reset(cursor()))
          options.resetStreamLiveTurnState()
        }
        void options.loadHistory()
      }
      if (live.initialMetadata.hydrationComplete) {
        return applyHydratedSubscriptionState(
          key,
          metadataGeneration,
          live.initialMetadata,
          live.activity,
          live.streamGeneration,
        )
      }
      if (options.ownershipHydrationRequired?.() !== false) {
        options.taskOwnership?.applySnapshot(
          metadataRunStatusSource(live.initialMetadata),
          false,
        )
      }
      scheduleMetadataHydration(
        lease,
        key,
        sequence,
        metadataHydration,
        metadataGeneration,
        live.activity,
        live.streamGeneration,
        signal,
      )
      // Fast ACK is authoritative for delivery registration. Deferred storage
      // metadata may refine task/workspace state later but cannot make history
      // or the real-time channel non-terminal.
      const taskOrInterruptLive = (
        snapshotTaskLive
        || options.isStreaming.value
        || options.hasActiveInterrupt.value
        || live.activity === 'foreground'
      )
      return {
        authoritative: true,
        live: taskOrInterruptLive || live.activity === 'background',
        backgroundOnly: live.activity === 'background' && !taskOrInterruptLive,
      }
    } catch (err: unknown) {
      console.warn('Session stream subscription failed:', err instanceof Error ? err.message : err)
      const cancelled = (
        !isCurrentSubscription(lease, key, sequence)
        || signal.aborted
        || isRpcAbort(err)
      )
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

  async function retrySessionMetadata(
    retryOptions: SessionMetadataRetryOptions = {},
  ): Promise<boolean> {
    const key = options.sessionKey.value
    if (!key) return false
    const lease = options.sessionReadLeaseReader.current()
    if (!lease) return false

    const metadataHydration = ++metadataHydrationSequence
    const metadataGeneration = options.beginSessionMetadataResolution?.(key)
    activeMetadataController?.abort()
    const controller = new AbortController()
    activeMetadataController = controller
    const externalSignal = retryOptions.signal
    const relayAbort = () => controller.abort()
    if (externalSignal?.aborted) controller.abort()
    else externalSignal?.addEventListener('abort', relayAbort, { once: true })

    const timeoutMs = Math.max(
      1,
      retryOptions.timeoutMs ?? SESSION_PHASE_ATTEMPT_BUDGET_MS,
    )
    const isCurrent = () => (
      metadataHydration === metadataHydrationSequence
      && key === options.sessionKey.value
      && options.sessionReadLeaseReader.current() === lease
      && !controller.signal.aborted
    )

    try {
      const [hydration, live] = await Promise.all([
        waitForMetadataRetry(
          lease.retryMetadata(),
          controller.signal,
          timeoutMs,
        ),
        lease.live,
      ])
      if (!isCurrent()) return false
      if (live.streamGeneration) {
        observeStreamGeneration({
          sessionKey: key,
          streamGeneration: live.streamGeneration,
          ...(live.reloadRequired === 'generationChanged'
            ? { replayGapReason: 'stream_generation_changed' }
            : {}),
        })
      }
      if (!hydration.hydrationComplete) {
        throw new Error('Session state hydration remained incomplete')
      }
      applyHydratedSubscriptionState(
        key,
        metadataGeneration,
        hydration,
        'unknown',
        live.streamGeneration,
      )
      return true
    } catch (cause) {
      if (isCurrent() && metadataGeneration !== undefined) {
        options.onSessionMetadataError?.(key, metadataGeneration)
      }
      if (isCurrent()) {
        console.warn(
          'Session metadata recovery failed:',
          cause instanceof Error ? cause.message : cause,
        )
      }
      return false
    } finally {
      externalSignal?.removeEventListener('abort', relayAbort)
      if (activeMetadataController === controller) {
        activeMetadataController = null
      }
    }
  }

  function cancelActiveSubscription() {
    ++subscriptionSequence
    ++metadataHydrationSequence
    activeSubscriptionController?.abort()
    activeSubscriptionController = null
    activeMetadataController?.abort()
    activeMetadataController = null
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
    streamGeneration,
    observeStreamGeneration,
    subscribeSession,
    retrySessionMetadata,
    unsubscribeSession,
    cancelActiveSubscription,
    applySessionRunState,
  }
}
