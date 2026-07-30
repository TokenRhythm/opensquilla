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
  sessionRunStatus: (source: ChatRunStatusSource | null | undefined) => ChatRunStatus
  startStreaming: () => void
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

const UNAVAILABLE_SUBSCRIPTION: SessionSubscriptionOutcome = {
  authoritative: false,
  live: false,
  backgroundOnly: false,
}

export function useChatSessionSubscription(options: UseChatSessionSubscriptionOptions) {
  const isHydrating = ref(false)
  let subscriptionAttempt = 0
  let activeSubscription: {
    key: string
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
    const key = options.sessionKey.value
    const sinceStreamSeq = options.lastStreamSeq.value
    const bootstrapGeneration = bootstrap?.generation ?? -1
    const bootstrapAttempt = bootstrap?.attempt ?? -1
    if (
      activeSubscription?.key === key
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
      sinceStreamSeq,
      token,
      controller,
      attemptContext,
    ).finally(() => {
      bootstrap?.signal.removeEventListener('abort', relayAbort)
    })
    activeSubscription = {
      key,
      sinceStreamSeq,
      bootstrapGeneration,
      bootstrapAttempt,
      token,
      outcome,
    }
    return outcome
  }

  function applyReplayCursor(res: SessionMessagesSubscribeResponse) {
    if (res.replay_complete === false) {
      options.lastStreamSeq.value = typeof res.current_stream_seq === 'number'
        ? Math.max(options.lastStreamSeq.value, res.current_stream_seq)
        : options.lastStreamSeq.value
      options.loadHistory()
    } else if (typeof res.current_stream_seq === 'number') {
      options.lastStreamSeq.value = Math.max(
        options.lastStreamSeq.value,
        res.current_stream_seq,
      )
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
      } | null | undefined
      const taskId = activeTask?.task_id || activeTask?.taskId
      if (taskId) options.activeStreamTaskId.value = taskId
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
          if (
            snapshot?.key === key
            && Array.isArray(snapshot.events)
            && typeof snapshot.current_stream_seq === 'number'
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
      if (subscribeResult.status === 'rejected') throw subscribeResult.reason
      const res = subscribeResult.value
      if (res && res.subscribed === false) {
        throw new Error('No subscription manager available')
      }
      applyReplayCursor(res)
      const hydrationComplete = (
        res.hydration_complete
        ?? res.hydrationComplete
        ?? true
      ) !== false
      if (hydrationComplete) {
        return applyHydratedSubscriptionState(key, metadataGeneration, res)
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
    if (
      LIVE_RUN_STATES.includes(current.status)
      && LIVE_RUN_STATES.includes(next.status)
      && current.task
      && next.task
    ) {
      const currentTaskId = current.task.task_id
        || current.task.taskId
        || current.task.turn_id
        || current.task.turnId
        || ''
      const nextTaskId = next.task.task_id
        || next.task.taskId
        || next.task.turn_id
        || next.task.turnId
        || ''
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
    subscribeSession,
    retrySessionMetadata,
    unsubscribeSession,
    cancelActiveSubscription,
    applySessionRunState,
  }
}
