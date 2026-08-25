import { ref, type Ref } from 'vue'
import type {
  ChatRunStatus,
  ChatRunStatusSource,
} from '@/types/chat'
import type {
  SessionProjectWorkspaceSnapshot,
  SessionMessagesSnapshotResponse,
  SessionMessagesSubscribeParams,
  SessionMessagesSubscribeResponse,
} from '@/types/rpc'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import { chatTaskId } from '@/composables/chat/useChatTaskOwnership'
import {
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  SESSION_SNAPSHOT_BUDGET_MS,
  isRpcAbort,
  isRpcTimeout,
  isStorageBusy,
  phaseCallOptions,
  phaseConnectionWaitOptions,
  phaseTimeoutMs,
  rpcErrorCode,
  type SessionBootstrapPhaseContext,
} from '@/composables/chat/sessionBootstrapContract'

type RpcClient = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<T>
}

export interface UseChatSessionSubscriptionOptions {
  rpc: RpcClient
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
  startStreaming: () => void
  /** Adopt durable task timing even when snapshot replay already opened the bubble. */
  reconcileStreamTaskClock?: (snapshot: {
    sessionKey: string
    taskId: string
    startedAt?: number | string | null
  }) => boolean | void
  loadHistory: () => void | Promise<unknown>
  resetStreamIdleTimer: () => void
  resetStreamLiveTurnState: () => void
  onLiveSnapshot?: (snapshot: SessionMessagesSnapshotResponse) => void
  onAuthoritativeIdle?: () => void
  onRunModeLock?: (
    lock: NonNullable<SessionMessagesSubscribeResponse['run_mode_lock']>,
  ) => void
  beginSessionMetadataResolution?: (key: string) => number
  onSessionMetadata?: (
    key: string,
    generation: number,
    metadata: {
      workspaceId?: string
      projectWorkspace?: SessionProjectWorkspaceSnapshot | null
    },
  ) => void
  onSessionMetadataError?: (key: string, generation: number) => void
  onSnapshot?: (snapshot: SessionMessagesSubscribeResponse) => void
}

const LIVE_RUN_STATES = ['queued', 'running', 'approval_pending']

export interface SessionSubscriptionOutcome {
  authoritative: boolean
  live: boolean
  backgroundOnly: boolean
  error?: unknown
  cancelled?: boolean
  skipSnapshotOnRetry?: boolean
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

export function useChatSessionSubscription(options: UseChatSessionSubscriptionOptions) {
  const isHydrating = ref(false)
  const streamGeneration = ref<string | null>(null)
  let subscriptionAttempt = 0
  let activeSubscription: {
    key: string
    sinceStreamGeneration: string | null
    sinceStreamSeq: number
    bootstrapGeneration: number
    bootstrapAttempt: number
    token: symbol
    outcome: Promise<SessionSubscriptionOutcome>
  } | null = null
  let activeController: AbortController | null = null
  let activeMetadataController: AbortController | null = null
  let metadataHydrationSequence = 0

  function subscribeSession(
    bootstrap?: SessionBootstrapPhaseContext,
  ): Promise<SessionSubscriptionOutcome> {
    if (!options.sessionKey.value) return Promise.resolve(UNAVAILABLE_SUBSCRIPTION)
    if (options.ownershipHydrationRequired?.() !== false) {
      options.taskOwnership?.beginHydration()
    }
    const key = options.sessionKey.value
    const sinceStreamGeneration = streamGeneration.value
    const sinceStreamSeq = options.lastStreamSeq.value
    const bootstrapGeneration = bootstrap?.generation ?? -1
    const bootstrapAttempt = bootstrap?.attempt ?? -1
    if (
      activeSubscription?.key === key
      && activeSubscription.sinceStreamGeneration === sinceStreamGeneration
      && activeSubscription.sinceStreamSeq === sinceStreamSeq
      && activeSubscription.bootstrapGeneration === bootstrapGeneration
      && activeSubscription.bootstrapAttempt === bootstrapAttempt
    ) {
      return activeSubscription.outcome
    }
    activeController?.abort()
    const controller = new AbortController()
    activeController = controller
    const relayAbort = () => controller.abort()
    if (bootstrap?.signal.aborted) controller.abort()
    else bootstrap?.signal.addEventListener('abort', relayAbort, { once: true })
    const attemptContext = bootstrap
      ? { ...bootstrap, signal: controller.signal }
      : undefined
    const token = Symbol('session-subscription')
    const outcome = runSubscription(
      key,
      sinceStreamGeneration,
      sinceStreamSeq,
      token,
      controller,
      attemptContext,
    ).finally(() => {
      bootstrap?.signal.removeEventListener('abort', relayAbort)
    })
    activeSubscription = {
      key,
      sinceStreamGeneration,
      sinceStreamSeq,
      bootstrapGeneration,
      bootstrapAttempt,
      token,
      outcome,
    }
    return outcome
  }

  function generationFrom(source: unknown): string | null {
    if (typeof source === 'string') return source || null
    if (!source || typeof source !== 'object') return null
    const envelope = source as {
      stream_generation?: unknown
      streamGeneration?: unknown
    }
    const value = envelope.stream_generation ?? envelope.streamGeneration
    return typeof value === 'string' && value ? value : null
  }

  /**
   * Observe a generation-bearing live event before applying its numeric cursor.
   * The event-handler integration calls this first so a restarted Gateway's low
   * sequence numbers are accepted instead of compared with the retired stream.
   */
  function observeStreamGeneration(source: unknown): boolean {
    const generation = generationFrom(source)
    if (!generation || generation === streamGeneration.value) return false
    const previous = streamGeneration.value
    streamGeneration.value = generation
    if (previous === null) {
      // A page can survive an in-place upgrade from a legacy Gateway which did
      // not expose generations.  In that case the client owns a numeric cursor
      // but cannot prove it belongs to the newly observed stream.  Reset when
      // the new stream is visibly behind, or explicitly reports a generation
      // gap; otherwise merely adopt the generation (the ordinary first
      // subscribe response has an equal/current cursor).
      const envelope = source && typeof source === 'object'
        ? source as {
            current_stream_seq?: unknown
            replay_gap_reason?: unknown
            stream_seq?: unknown
          }
        : null
      const sequence = envelope?.stream_seq ?? envelope?.current_stream_seq
      const newStreamIsBehind = typeof sequence === 'number'
        && Number.isFinite(sequence)
        && sequence < options.lastStreamSeq.value
      const generationGap = envelope?.replay_gap_reason === 'stream_generation_changed'
      if (!newStreamIsBehind && !generationGap) return false
    }
    options.lastStreamSeq.value = 0
    options.resetStreamLiveTurnState()
    return true
  }

  function reconcileSubscriptionGeneration(
    res: SessionMessagesSubscribeResponse,
    sinceStreamGeneration: string | null,
  ): boolean {
    const received = generationFrom(res)
    // Keep the ACK envelope intact: the legacy -> generation-aware upgrade
    // path needs its current sequence/replay-gap fields to decide whether a
    // pre-existing numeric cursor belongs to the retired stream. Passing only
    // the generation string would adopt the generation while still rejecting
    // every low-sequence event from the restarted Gateway.
    if (received) return observeStreamGeneration(res)
    if (sinceStreamGeneration === null) return false

    // A mixed-version reconnect can land on an older Gateway which ignores
    // generation fields. Treat that capability downgrade as a new stream so
    // its lower sequence numbers are not hidden behind the modern cursor.
    streamGeneration.value = null
    options.lastStreamSeq.value = 0
    options.resetStreamLiveTurnState()
    return true
  }

  function applyReplayCursor(
    res: SessionMessagesSubscribeResponse,
    generationReset: boolean,
  ) {
    const current = typeof res.current_stream_seq === 'number'
      && Number.isFinite(res.current_stream_seq)
      ? Math.max(0, res.current_stream_seq)
      : null
    if (res.replay_complete === false || generationReset) {
      if (current !== null) {
        options.lastStreamSeq.value = generationReset
          && options.lastStreamSeq.value === 0
          ? current
          : Math.max(options.lastStreamSeq.value, current)
      }
      options.loadHistory()
    } else if (current !== null) {
      options.lastStreamSeq.value = Math.max(options.lastStreamSeq.value, current)
    }
  }

  function applyHydratedSubscriptionState(
    key: string,
    metadataGeneration: number | undefined,
    res: SessionMessagesSubscribeResponse,
  ): SessionSubscriptionOutcome {
    if (metadataGeneration !== undefined) {
      options.onSessionMetadata?.(key, metadataGeneration, {
        workspaceId: res.workspaceId,
        projectWorkspace: res.projectWorkspace,
      })
    }
    const runModeLock = res.run_mode_lock || res.runModeLock
    if (runModeLock && typeof runModeLock === 'object') {
      options.onRunModeLock?.(runModeLock)
    }
    options.onSnapshot?.(res)
    options.taskOwnership?.applySnapshot(res, true)
    // Do not clear an acceptance-result-unknown Stop from an idle snapshot.
    // The subscription can race ahead of the original ingress commit, so only
    // the matching send transaction (receipt/rejection) or an explicit session
    // reset may release that latch.  Its idempotent replay must still inherit
    // the Stop intent and abort the exact accepted task once the receipt exists.
    applySessionRunState(res)
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
    reconcileActiveTaskGroups(res)
    if (liveTaskSnapshot && !options.isStreaming.value) {
      options.startStreaming()
      // startStreaming establishes the live bubble with a generic running
      // placeholder. Restore the authoritative active-task payload (including
      // steer_capability) that came from hydration instead of waiting for a
      // later task.running event to repair it.
      applySessionRunState(res)
    }
    if (liveTaskSnapshot) {
      const activeTask = (res.active_task || res.activeTask) as {
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
    const taskOrInterruptLive = liveTaskSnapshot || options.hasActiveInterrupt.value
    const groupLive = options.activeTaskGroups.value.size > 0
    const outcome = {
      authoritative: true,
      live: taskOrInterruptLive || groupLive,
      backgroundOnly: groupLive && !taskOrInterruptLive,
    }
    if (!outcome.live) options.onAuthoritativeIdle?.()
    return outcome
  }

  function scheduleDeferredHydration(
    key: string,
    attempt: number,
    metadataHydration: number,
    metadataGeneration: number | undefined,
    bootstrap: SessionBootstrapPhaseContext,
  ) {
    void (async () => {
      try {
        await bootstrap.waitForCriticalRequestsQueued?.()
        if (
          attempt !== subscriptionAttempt
          || metadataHydration !== metadataHydrationSequence
          || key !== options.sessionKey.value
          || bootstrap.signal.aborted
        ) return
        // Storage-backed metadata is deliberately outside the critical
        // history/live bootstrap. Once their request frames are queued it
        // receives its own bounded window; slow history must not keep a healthy
        // project session permanently unresolved.
        const hydrationDeadlineAt = Date.now() + SESSION_PHASE_ATTEMPT_BUDGET_MS
        const hydrationContext = {
          ...bootstrap,
          deadlineAt: hydrationDeadlineAt,
          attemptDeadlineAt: hydrationDeadlineAt,
        }
        const hydration = await options.rpc.call<SessionMessagesSubscribeResponse>(
          'sessions.messages.hydrate',
          { key },
          phaseCallOptions(hydrationContext, 'sessions.messages.hydrate'),
        )
        if (
          attempt !== subscriptionAttempt
          || metadataHydration !== metadataHydrationSequence
          || key !== options.sessionKey.value
          || bootstrap.signal.aborted
        ) return
        const complete = (
          hydration.hydration_complete
          ?? hydration.hydrationComplete
          ?? true
        ) !== false
        if (!complete) throw new Error('Session state hydration remained incomplete')
        applyHydratedSubscriptionState(key, metadataGeneration, hydration)
      } catch (cause) {
        if (
          attempt === subscriptionAttempt
          && metadataHydration === metadataHydrationSequence
          && key === options.sessionKey.value
          && !bootstrap.signal.aborted
        ) {
          if (metadataGeneration !== undefined) {
            options.onSessionMetadataError?.(key, metadataGeneration)
          }
          console.warn(
            'Session metadata hydration failed:',
            cause instanceof Error ? cause.message : cause,
          )
        }
      }
    })()
  }

  async function runSubscription(
    key: string,
    sinceStreamGeneration: string | null,
    sinceStreamSeq: number,
    token: symbol,
    controller: AbortController,
    bootstrap?: SessionBootstrapPhaseContext,
  ): Promise<SessionSubscriptionOutcome> {
    const attempt = ++subscriptionAttempt
    const metadataHydration = ++metadataHydrationSequence
    const metadataGeneration = options.beginSessionMetadataResolution?.(key)
    let skipSnapshotOnRetry = Boolean(bootstrap?.skipSnapshot)
    if (sinceStreamSeq === 0) isHydrating.value = true
    try {
      if (bootstrap) {
        await options.rpc.waitForConnection(
          phaseTimeoutMs(bootstrap, 'sessions.messages.subscribe'),
          bootstrap.signal,
          phaseConnectionWaitOptions(),
        )
      } else {
        await options.rpc.waitForConnection()
      }
      if (attempt !== subscriptionAttempt || key !== options.sessionKey.value) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }
      const params: SessionMessagesSubscribeParams = {
        key,
        ...(sinceStreamGeneration
          ? { since_stream_generation: sinceStreamGeneration }
          : {}),
        since_stream_seq: sinceStreamSeq,
        fast_ack: true,
      }
      const onLiveSnapshot = options.onLiveSnapshot
      const snapshotRequired = Boolean(
        onLiveSnapshot && !bootstrap?.skipSnapshot,
      )
      let subscribeSocketGeneration: number | null = null
      let snapshotSocketGeneration: number | null = null
      let liveFramesMarked = false
      const markLiveFramesSent = () => {
        if (
          !bootstrap
          || liveFramesMarked
          || subscribeSocketGeneration === null
          || (snapshotRequired && snapshotSocketGeneration === null)
          || (
            snapshotSocketGeneration !== null
            && snapshotSocketGeneration !== subscribeSocketGeneration
          )
        ) return
        liveFramesMarked = true
        bootstrap.markLiveSubscribeSent?.(subscribeSocketGeneration)
      }
      const subscribeCallOptions = bootstrap
        ? {
            ...phaseCallOptions(bootstrap, 'sessions.messages.subscribe'),
            onSent: (socketGeneration: number) => {
              subscribeSocketGeneration = socketGeneration
              markLiveFramesSent()
            },
          }
        : undefined
      const subscribePromise = bootstrap
        ? options.rpc.call<SessionMessagesSubscribeResponse>(
            'sessions.messages.subscribe',
            params,
            subscribeCallOptions,
          )
        : options.rpc.call<SessionMessagesSubscribeResponse>(
            'sessions.messages.subscribe',
            params,
          )
      // Pipeline the in-memory snapshot directly behind subscribe. Only after
      // both frames are on the wire may history enter the serialized queue:
      // subscribe → snapshot → history. Slow storage metadata is deferred.
      const snapshotPromise = snapshotRequired
        ? (
            bootstrap
              ? options.rpc.call<SessionMessagesSnapshotResponse>(
                  'sessions.messages.snapshot',
                  { key },
                  {
                    ...phaseCallOptions(
                      bootstrap,
                      'sessions.messages.snapshot',
                      SESSION_SNAPSHOT_BUDGET_MS,
                    ),
                    onSent: (socketGeneration: number) => {
                      snapshotSocketGeneration = socketGeneration
                      markLiveFramesSent()
                    },
                  },
                )
              : options.rpc.call<SessionMessagesSnapshotResponse>(
                  'sessions.messages.snapshot',
                  { key },
                )
          )
        : null

      const [subscribeResult, snapshotResult] = await Promise.allSettled([
        subscribePromise,
        snapshotPromise,
      ] as const)
      if (attempt !== subscriptionAttempt || key !== options.sessionKey.value) {
        return { ...UNAVAILABLE_SUBSCRIPTION, cancelled: true }
      }

      if (subscribeResult.status === 'rejected') throw subscribeResult.reason
      const res = subscribeResult.value
      if (res && res.subscribed === false) {
        throw new Error('No subscription manager available')
      }
      const generationReset = reconcileSubscriptionGeneration(
        res,
        sinceStreamGeneration,
      )

      let snapshotTaskLive = false
      if (snapshotPromise) {
        skipSnapshotOnRetry = true
        if (snapshotResult.status === 'rejected') {
          const error = snapshotResult.reason
          if (
            bootstrap
            && (
              bootstrap.signal.aborted
              || isRpcAbort(error)
              || isRpcTimeout(error)
              || isStorageBusy(error)
              || rpcErrorCode(error) !== 'METHOD_NOT_FOUND'
            )
          ) {
            throw error
          }
          // Older gateways do not expose the snapshot RPC. Continue with the
          // bounded replay protocol so mixed-version client updates still work.
        } else {
          const snapshot = snapshotResult.value
          const snapshotGeneration = generationFrom(snapshot)
          if (
            snapshot?.key === key
            && Array.isArray(snapshot.events)
            && typeof snapshot.current_stream_seq === 'number'
            && (
              !snapshotGeneration
              || !streamGeneration.value
              || snapshotGeneration === streamGeneration.value
            )
            // Events delivered after registration are newer than a late
            // snapshot response. Never reset the live surface behind them.
            && snapshot.current_stream_seq >= options.lastStreamSeq.value
          ) {
            onLiveSnapshot?.(snapshot)
            options.lastStreamSeq.value = Math.max(0, snapshot.current_stream_seq)
            snapshotTaskLive = Boolean(snapshot.task_id)
          }
        }
      }
      applyReplayCursor(res, generationReset)
      const hydrationComplete = (
        res.hydration_complete
        ?? res.hydrationComplete
        ?? true
      ) !== false
      if (hydrationComplete) {
        return applyHydratedSubscriptionState(key, metadataGeneration, res)
      }
      if (options.ownershipHydrationRequired?.() !== false) {
        options.taskOwnership?.applySnapshot(res, false)
      }
      if (bootstrap) {
        scheduleDeferredHydration(
          key,
          attempt,
          metadataHydration,
          metadataGeneration,
          bootstrap,
        )
      } else {
        const hydration = await options.rpc.call<SessionMessagesSubscribeResponse>(
          'sessions.messages.hydrate',
          { key },
        )
        const complete = (
          hydration.hydration_complete
          ?? hydration.hydrationComplete
          ?? true
        ) !== false
        if (!complete) throw new Error('Session state hydration remained incomplete')
        return applyHydratedSubscriptionState(
          key,
          metadataGeneration,
          { ...res, ...hydration },
        )
      }
      // Fast ACK is authoritative for delivery registration. Deferred storage
      // metadata may refine task/workspace state later but cannot make history
      // or the real-time channel non-terminal.
      const taskOrInterruptLive = (
        snapshotTaskLive
        || options.isStreaming.value
        || options.hasActiveInterrupt.value
      )
      return {
        authoritative: true,
        live: taskOrInterruptLive,
        backgroundOnly: false,
      }
    } catch (err: unknown) {
      console.warn('Session stream subscription failed:', err instanceof Error ? err.message : err)
      const cancelled = (
        attempt !== subscriptionAttempt
        || key !== options.sessionKey.value
        || bootstrap?.signal.aborted
        || isRpcAbort(err)
      )
      if (
        metadataGeneration !== undefined
        && !cancelled
        && (!bootstrap || bootstrap.attempt === 1)
        && attempt === subscriptionAttempt
        && key === options.sessionKey.value
      ) {
        options.onSessionMetadataError?.(key, metadataGeneration)
      }
      return {
        ...UNAVAILABLE_SUBSCRIPTION,
        error: err,
        cancelled,
        skipSnapshotOnRetry,
      }
    } finally {
      if (attempt === subscriptionAttempt) isHydrating.value = false
      if (activeSubscription?.token === token) activeSubscription = null
      if (activeController === controller) activeController = null
    }
  }

  async function retrySessionMetadata(
    callOptions: RpcCallOptions = {},
  ): Promise<boolean> {
    const key = options.sessionKey.value
    if (!key) return false

    const metadataHydration = ++metadataHydrationSequence
    const metadataGeneration = options.beginSessionMetadataResolution?.(key)
    activeMetadataController?.abort()
    const controller = new AbortController()
    activeMetadataController = controller
    const externalSignal = callOptions.signal
    const relayAbort = () => controller.abort()
    if (externalSignal?.aborted) controller.abort()
    else externalSignal?.addEventListener('abort', relayAbort, { once: true })

    const deadlineAt = Date.now() + Math.max(
      1,
      callOptions.timeoutMs ?? SESSION_PHASE_ATTEMPT_BUDGET_MS,
    )
    const isCurrent = () => (
      metadataHydration === metadataHydrationSequence
      && key === options.sessionKey.value
      && !controller.signal.aborted
    )

    try {
      await options.rpc.waitForConnection(
        Math.max(1, deadlineAt - Date.now()),
        controller.signal,
        {
          timeoutAction: callOptions.timeoutAction ?? 'reconnect',
          abortAction: callOptions.abortAction ?? 'reconnect',
        },
      )
      if (!isCurrent()) return false
      const hydration = await options.rpc.call<SessionMessagesSubscribeResponse>(
        'sessions.messages.hydrate',
        { key },
        {
          ...callOptions,
          timeoutMs: Math.max(1, deadlineAt - Date.now()),
          signal: controller.signal,
          timeoutAction: callOptions.timeoutAction ?? 'reconnect',
          abortAction: callOptions.abortAction ?? 'reconnect',
        },
      )
      if (!isCurrent()) return false
      const complete = (
        hydration.hydration_complete
        ?? hydration.hydrationComplete
        ?? true
      ) !== false
      if (!complete) throw new Error('Session state hydration remained incomplete')
      applyHydratedSubscriptionState(key, metadataGeneration, hydration)
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
    ++subscriptionAttempt
    ++metadataHydrationSequence
    activeController?.abort()
    activeController = null
    activeMetadataController?.abort()
    activeMetadataController = null
    activeSubscription = null
    isHydrating.value = false
  }

  async function unsubscribeSession(key = options.sessionKey.value) {
    cancelActiveSubscription()
    if (!key) return
    try {
      await options.rpc.call(
        'sessions.messages.unsubscribe',
        { key },
        {
          timeoutMs: 2_000,
          timeoutAction: 'reject',
          abortAction: 'reject',
        },
      )
    } catch {
      // Unsubscribe is best-effort during route changes and unmount.
    }
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

  function reconcileActiveTaskGroups(res: SessionMessagesSubscribeResponse) {
    const snapshot = res.active_task_group_ids || res.activeTaskGroupIds
    if (!Array.isArray(snapshot)) return
    options.activeTaskGroups.value = new Set(
      snapshot.filter((groupId): groupId is string => typeof groupId === 'string' && Boolean(groupId)),
    )
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
