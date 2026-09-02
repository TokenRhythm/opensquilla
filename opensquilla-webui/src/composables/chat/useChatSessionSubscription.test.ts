import { ref, type Ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import {
  useChatSessionSubscription,
  type UseChatSessionSubscriptionOptions,
} from './useChatSessionSubscription'
import { useChatTaskOwnership, type ChatTaskOwnershipApi } from './useChatTaskOwnership'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import {
  createSessionReadLifecycle,
  SessionReadSessionMissingError,
  type SessionReadHistoryPage,
  type SessionReadLease,
  type SessionReadLive,
  type SessionReadMetadata,
  type SessionReadPort,
  type SessionReadPortLive,
  type SessionReadSnapshot,
} from '@/modules/sessionReadLifecycle'
import { createConversationSubscriptionLifecycle } from '@/modules/conversationSubscriptionLifecycle'
import type {
  ChatRunStatus,
  ChatRunStatusSource,
  ChatRunStatusState,
} from '@/types/chat'

const KEY = 'agent:main:webchat:test'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function metadata(
  overrides: Partial<SessionReadMetadata> = {},
): SessionReadMetadata {
  return {
    sessionKey: KEY,
    workspaceId: null,
    projectWorkspace: null,
    projectWorkspaceDeferred: false,
    activeTaskGroupIds: [],
    runModeLock: {
      locked: false,
      runMode: null,
      source: null,
      additional: {},
    },
    pendingUserInputs: [],
    collaboration: null,
    routing: null,
    currentPlan: null,
    activePlanRun: null,
    goal: null,
    goalSnapshotStreamSeq: null,
    tasks: [],
    activeTask: null,
    lastTask: null,
    runStatus: 'idle',
    queuedTaskIds: [],
    epoch: null,
    hydrationComplete: true,
    deferredFields: [],
    additional: {},
    ...overrides,
  }
}

function live(overrides: Partial<SessionReadLive> = {}): SessionReadLive {
  return {
    sessionKey: KEY,
    activity: 'idle',
    activeTaskId: null,
    streamGeneration: 'generation-1',
    initialMetadata: metadata(),
    snapshot: null,
    reloadRequired: null,
    ...overrides,
  }
}

const EMPTY_HISTORY: SessionReadHistoryPage = {
  messages: [],
  hasMore: false,
  oldestCursor: null,
  newestCursor: null,
  scope: 'complete',
  loadedCount: 0,
  pageSize: 100,
  canonicalAvailable: true,
  canonicalComplete: true,
  compactionSummaries: [],
  turnOutcomes: [],
  additional: {},
}

function leaseFixture(options: {
  live?: SessionReadLive | Promise<SessionReadLive>
  metadata?: SessionReadMetadata | Promise<SessionReadMetadata>
  retryMetadata?: () => Promise<SessionReadMetadata>
  criticalRequestsQueued?: Promise<void>
} = {}) {
  const metadataPromise = Promise.resolve(options.metadata ?? metadata())
  const retryMetadata = vi.fn(
    options.retryMetadata ?? (async () => metadataPromise),
  )
  const close = vi.fn(async () => {})
  const lease: SessionReadLease = {
    criticalRequestsQueued: options.criticalRequestsQueued ?? Promise.resolve(),
    live: Promise.resolve(options.live ?? live()),
    metadata: metadataPromise,
    history: {
      latest: async () => EMPTY_HISTORY,
      before: async () => EMPTY_HISTORY,
      after: async () => EMPTY_HISTORY,
    },
    retryMetadata,
    close,
  }
  return { lease, retryMetadata, close }
}

function runStatus(
  source: ChatRunStatusSource | null | undefined,
): ChatRunStatus {
  const value = String(source?.runStatus ?? source?.run_status ?? 'idle').toLowerCase()
  const known: ChatRunStatusState[] = [
    'idle',
    'queued',
    'running',
    'approval_pending',
    'interrupted',
    'failed',
    'timeout',
    'cancelled',
  ]
  const status = known.includes(value as ChatRunStatusState)
    ? value as ChatRunStatusState
    : 'idle'
  return {
    status,
    label: status,
    task: source?.activeTask ?? source?.active_task ?? null,
  }
}

interface HarnessOptions {
  reader?: { current(): SessionReadLease | null }
  sessionKey?: Ref<string>
  lastStreamSeq?: Ref<number>
  runStatus?: Ref<ChatRunStatus>
  isStreaming?: Ref<boolean>
  hasActiveInterrupt?: Ref<boolean>
  activeStreamTaskId?: Ref<string>
  activeTaskGroups?: Ref<Set<string>>
  taskOwnership?: ChatTaskOwnershipApi
  acceptanceStopPending?: Ref<boolean>
  startStreaming?: UseChatSessionSubscriptionOptions['startStreaming']
  reconcileStreamTaskClock?: UseChatSessionSubscriptionOptions['reconcileStreamTaskClock']
  loadHistory?: UseChatSessionSubscriptionOptions['loadHistory']
  resetStreamLiveTurnState?: UseChatSessionSubscriptionOptions['resetStreamLiveTurnState']
  onLiveSnapshot?: UseChatSessionSubscriptionOptions['onLiveSnapshot']
  onAuthoritativeIdle?: UseChatSessionSubscriptionOptions['onAuthoritativeIdle']
  onRunModeLock?: UseChatSessionSubscriptionOptions['onRunModeLock']
  beginSessionMetadataResolution?: UseChatSessionSubscriptionOptions['beginSessionMetadataResolution']
  onSessionMetadata?: UseChatSessionSubscriptionOptions['onSessionMetadata']
  onSessionMetadataError?: UseChatSessionSubscriptionOptions['onSessionMetadataError']
  onSessionMissing?: UseChatSessionSubscriptionOptions['onSessionMissing']
  onSnapshot?: UseChatSessionSubscriptionOptions['onSnapshot']
}

function harness(
  initialLease: SessionReadLease | null = leaseFixture().lease,
  options: HarnessOptions = {},
) {
  let currentLease = initialLease
  const reader = options.reader ?? { current: vi.fn(() => currentLease) }
  const sessionKey = options.sessionKey ?? ref(KEY)
  const lastStreamSeq = options.lastStreamSeq ?? ref(0)
  const currentRunStatus = options.runStatus ?? ref<ChatRunStatus>({
    status: 'idle',
    label: 'idle',
    task: null,
  })
  const isStreaming = options.isStreaming ?? ref(false)
  const hasActiveInterrupt = options.hasActiveInterrupt ?? ref(false)
  const activeStreamTaskId = options.activeStreamTaskId ?? ref('')
  const activeTaskGroups = options.activeTaskGroups ?? ref(new Set<string>())
  const startStreaming = options.startStreaming ?? vi.fn(() => {
    isStreaming.value = true
  })
  const resetStreamLiveTurnState = options.resetStreamLiveTurnState ?? vi.fn(() => {
    isStreaming.value = false
    activeStreamTaskId.value = ''
  })
  const loadHistory = options.loadHistory ?? vi.fn()
  const onAuthoritativeIdle = options.onAuthoritativeIdle ?? vi.fn()
  const api = useChatSessionSubscription({
    sessionReadLeaseReader: reader,
    conversationRuntime: createConversationRuntime(),
    sessionKey,
    lastStreamSeq,
    runStatus: currentRunStatus,
    isStreaming,
    hasActiveInterrupt,
    activeStreamTaskId,
    activeTaskGroups,
    taskOwnership: options.taskOwnership,
    acceptanceStopPending: options.acceptanceStopPending,
    sessionRunStatus: runStatus,
    startStreaming,
    reconcileStreamTaskClock: options.reconcileStreamTaskClock,
    loadHistory,
    resetStreamIdleTimer: vi.fn(),
    resetStreamLiveTurnState,
    onLiveSnapshot: options.onLiveSnapshot,
    onAuthoritativeIdle,
    onRunModeLock: options.onRunModeLock,
    beginSessionMetadataResolution: options.beginSessionMetadataResolution,
    onSessionMetadata: options.onSessionMetadata,
    onSessionMetadataError: options.onSessionMetadataError,
    onSessionMissing: options.onSessionMissing,
    onSnapshot: options.onSnapshot,
  })
  return {
    api,
    reader,
    setLease(next: SessionReadLease | null) { currentLease = next },
    sessionKey,
    lastStreamSeq,
    runStatus: currentRunStatus,
    isStreaming,
    activeStreamTaskId,
    activeTaskGroups,
    startStreaming,
    resetStreamLiveTurnState,
    loadHistory,
    onAuthoritativeIdle,
  }
}

describe('useChatSessionSubscription domain lease', () => {
  it('fences only this consumer and leaves the shared lease open', async () => {
    const pendingLive = deferred<SessionReadLive>()
    const fixture = leaseFixture({ live: pendingLive.promise })
    const subject = harness(fixture.lease)

    const pending = subject.api.subscribeSession()
    await Promise.resolve()
    await subject.api.unsubscribeSession()
    pendingLive.resolve(live())

    await expect(pending).resolves.toMatchObject({
      authoritative: false,
      cancelled: true,
    })
    expect(subject.reader.current).toHaveBeenCalled()
    expect(fixture.close).not.toHaveBeenCalled()
  })

  it('reports unavailable when bootstrap has not installed a lease', async () => {
    await expect(harness(null).api.subscribeSession()).resolves.toEqual({
      authoritative: false,
      live: false,
      backgroundOnly: false,
    })
  })

  it('projects only a current domain missing failure as terminal session absence', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const currentLive = deferred<SessionReadLive>()
      const onSessionMissing = vi.fn()
      const current = harness(leaseFixture({ live: currentLive.promise }).lease, {
        onSessionMissing,
      })
      const currentResult = current.api.subscribeSession()
      currentLive.reject(new SessionReadSessionMissingError('session missing'))

      await expect(currentResult).resolves.toMatchObject({
        authoritative: false,
        cancelled: false,
        sessionMissing: true,
      })
      expect(onSessionMissing).toHaveBeenCalledOnce()
      expect(onSessionMissing).toHaveBeenCalledWith(KEY)

      const staleLive = deferred<SessionReadLive>()
      const staleKey = ref(KEY)
      const stale = harness(leaseFixture({ live: staleLive.promise }).lease, {
        sessionKey: staleKey,
        onSessionMissing,
      })
      const staleResult = stale.api.subscribeSession()
      staleKey.value = 'agent:main:webchat:successor'
      staleLive.reject(new SessionReadSessionMissingError('stale session missing'))

      await expect(staleResult).resolves.toMatchObject({
        authoritative: false,
        cancelled: true,
        sessionMissing: false,
      })
      expect(onSessionMissing).toHaveBeenCalledOnce()
    } finally {
      warn.mockRestore()
    }
  })

  it('preserves missing semantics through real lifecycle auto-close', async () => {
    const missingLive = deferred<SessionReadPortLive>()
    const close = vi.fn(async () => {})
    const port: SessionReadPort = {
      open: request => ({
        criticalRequestsQueued: Promise.resolve(),
        live: missingLive.promise,
        metadata: Promise.resolve(metadata({ sessionKey: request.sessionKey })),
        readHistory: async () => EMPTY_HISTORY,
        retryMetadata: async () => metadata({ sessionKey: request.sessionKey }),
        close,
      }),
    }
    const lifecycle = createSessionReadLifecycle({
      port,
      runtime: createConversationRuntime(),
      subscriptions: createConversationSubscriptionLifecycle(),
    })
    const lease = lifecycle.open({ sessionKey: KEY })
    const onSessionMissing = vi.fn()
    const subject = harness(lease, {
      reader: lifecycle,
      onSessionMissing,
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const result = subject.api.subscribeSession()
      missingLive.reject(new SessionReadSessionMissingError('session missing'))

      await expect(result).resolves.toMatchObject({
        authoritative: false,
        cancelled: false,
        sessionMissing: true,
      })
      expect(onSessionMissing).toHaveBeenCalledWith(KEY)
      await vi.waitFor(() => expect(close).toHaveBeenCalledOnce())
      expect(lifecycle.current()).toBeNull()
    } finally {
      warn.mockRestore()
    }
  })

  it('projects live task, snapshot, run mode, task clock, and steer capability', async () => {
    const snapshot: SessionReadSnapshot = {
      sessionKey: KEY,
      taskId: 'task-live',
      events: [],
    }
    const initialMetadata = metadata({
      runStatus: 'running',
      activeTask: {
        taskId: 'task-live',
        status: 'running',
        startedAt: 90_000,
        steerCapability: {
          mode: 'same_turn',
          expected_turn_id: 'turn-live',
        },
      },
      runModeLock: {
        locked: true,
        runMode: 'safe',
        source: 'task',
        additional: {},
      },
    })
    const onLiveSnapshot = vi.fn()
    const onRunModeLock = vi.fn()
    const onSnapshot = vi.fn()
    const reconcileStreamTaskClock = vi.fn()
    const subject = harness(leaseFixture({
      live: live({
        activity: 'foreground',
        activeTaskId: 'task-live',
        initialMetadata,
        snapshot,
      }),
      metadata: initialMetadata,
    }).lease, {
      onLiveSnapshot,
      onRunModeLock,
      onSnapshot,
      reconcileStreamTaskClock,
    })

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: true,
      backgroundOnly: false,
    })
    expect(onLiveSnapshot).toHaveBeenCalledWith(snapshot)
    expect(onRunModeLock).toHaveBeenCalledWith(initialMetadata.runModeLock)
    expect(onSnapshot).toHaveBeenCalledWith(initialMetadata, 'generation-1')
    expect(subject.startStreaming).toHaveBeenCalledWith(90_000)
    expect(subject.activeStreamTaskId.value).toBe('task-live')
    expect(reconcileStreamTaskClock).toHaveBeenCalledWith({
      sessionKey: KEY,
      taskId: 'task-live',
      startedAt: 90_000,
    })
    expect(subject.runStatus.value.task).toMatchObject({
      taskId: 'task-live',
      steerCapability: { mode: 'same_turn' },
    })
  })

  it('preserves rich task capability across compact lifecycle state', () => {
    const currentRunStatus = ref<ChatRunStatus>({
      status: 'running',
      label: 'running',
      task: {
        taskId: 'task-current',
        status: 'running',
        steerCapability: {
          mode: 'same_turn',
          expected_turn_id: 'turn-current',
        },
      },
    })
    const subject = harness(null, { runStatus: currentRunStatus })

    subject.api.applySessionRunState({
      runStatus: 'running',
      activeTask: { taskId: 'task-current', status: 'running' },
    })

    expect(currentRunStatus.value.task).toMatchObject({
      taskId: 'task-current',
      steerCapability: { expected_turn_id: 'turn-current' },
    })
  })

  it('rejects a task and snapshot already settled by durable history', async () => {
    const taskOwnership = useChatTaskOwnership()
    taskOwnership.noteTerminal('task-settled')
    const initialMetadata = metadata({
      runStatus: 'running',
      activeTask: { taskId: 'task-settled', status: 'running' },
      activeTaskGroupIds: ['stale-group'],
    })
    const onLiveSnapshot = vi.fn()
    const subject = harness(leaseFixture({
      live: live({
        initialMetadata,
        snapshot: { sessionKey: KEY, taskId: 'task-settled', events: [] },
      }),
      metadata: initialMetadata,
    }).lease, { taskOwnership, onLiveSnapshot })

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: false,
      backgroundOnly: false,
    })
    expect(onLiveSnapshot).not.toHaveBeenCalled()
    expect(subject.startStreaming).not.toHaveBeenCalled()
    expect(subject.runStatus.value.status).toBe('idle')
    expect([...subject.activeTaskGroups.value]).toEqual([])
  })

  it('keeps interrupt and unknown-acceptance Stop intent across late idle state', async () => {
    const acceptanceStopPending = ref(true)
    const subject = harness(leaseFixture().lease, {
      isStreaming: ref(true),
      hasActiveInterrupt: ref(true),
      acceptanceStopPending,
    })

    const outcome = await subject.api.subscribeSession()

    expect(subject.resetStreamLiveTurnState).not.toHaveBeenCalled()
    expect(subject.runStatus.value.status).toBe('approval_pending')
    expect(outcome.live).toBe(true)
    expect(acceptanceStopPending.value).toBe(true)
  })

  it('clears a stale live bubble when authoritative metadata is idle', async () => {
    const subject = harness(leaseFixture().lease, { isStreaming: ref(true) })

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: false,
      backgroundOnly: false,
    })
    expect(subject.resetStreamLiveTurnState).toHaveBeenCalledOnce()
    expect(subject.onAuthoritativeIdle).toHaveBeenCalledOnce()
  })

  it('stays hydrating until the initial live projection resolves', async () => {
    const pendingLive = deferred<SessionReadLive>()
    const subject = harness(leaseFixture({ live: pendingLive.promise }).lease)
    const pending = subject.api.subscribeSession()
    await Promise.resolve()

    expect(subject.api.isHydrating.value).toBe(true)
    pendingLive.resolve(live())
    await pending
    expect(subject.api.isHydrating.value).toBe(false)
  })

  it('uses fast metadata immediately and hydrates complete metadata independently', async () => {
    const initialMetadata = metadata({
      hydrationComplete: false,
      deferredFields: ['workspaceId'],
      runStatus: 'running',
      activeTask: { taskId: 'task-fast', status: 'running' },
    })
    const complete = deferred<SessionReadMetadata>()
    const taskOwnership = useChatTaskOwnership()
    const onSessionMetadata = vi.fn()
    const onSnapshot = vi.fn()
    const subject = harness(leaseFixture({
      live: live({
        activity: 'foreground',
        activeTaskId: 'task-fast',
        initialMetadata,
      }),
      metadata: complete.promise,
    }).lease, {
      taskOwnership,
      beginSessionMetadataResolution: () => 7,
      onSessionMetadata,
      onSnapshot,
    })

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: true,
      backgroundOnly: false,
    })
    expect(taskOwnership.hydrationResolved.value).toBe(false)
    expect(onSessionMetadata).not.toHaveBeenCalled()

    const hydrated = metadata({
      workspaceId: 'project-ready',
      runStatus: 'running',
      activeTask: { taskId: 'task-fast', status: 'running' },
    })
    complete.resolve(hydrated)
    await vi.waitFor(() => expect(onSessionMetadata).toHaveBeenCalledWith(
      KEY,
      7,
      hydrated,
    ))
    expect(onSnapshot).toHaveBeenCalledWith(hydrated, 'generation-1')
    expect(taskOwnership.hydrationResolved.value).toBe(true)
  })

  it('syncs a restarted lease generation before no-event hydration reconciliation', async () => {
    const complete = deferred<SessionReadMetadata>()
    const onSnapshot = vi.fn()
    const subject = harness(leaseFixture({
      live: live({
        streamGeneration: 'generation-2',
        reloadRequired: 'generationChanged',
        initialMetadata: metadata({
          hydrationComplete: false,
          deferredFields: ['pendingUserInputs', 'goalSnapshotStreamSeq'],
        }),
      }),
      metadata: complete.promise,
    }).lease, {
      lastStreamSeq: ref(100),
      onSnapshot,
    })
    subject.api.observeStreamGeneration({ streamGeneration: 'generation-1' })

    await expect(subject.api.subscribeSession()).resolves.toMatchObject({
      authoritative: true,
    })
    expect(subject.api.streamGeneration.value).toBe('generation-2')
    expect(subject.lastStreamSeq.value).toBe(0)

    const restartedMetadata = metadata({ goalSnapshotStreamSeq: 0 })
    complete.resolve(restartedMetadata)
    await vi.waitFor(() => expect(onSnapshot).toHaveBeenCalledWith(
      restartedMetadata,
      'generation-2',
    ))
  })

  it('honors background activity even before task-group metadata is complete', async () => {
    const subject = harness(leaseFixture({
      live: live({
        activity: 'background',
        initialMetadata: metadata({ hydrationComplete: false }),
      }),
      metadata: new Promise<SessionReadMetadata>(() => {}),
    }).lease)

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: true,
      backgroundOnly: true,
    })
  })

  it('reconciles task groups as background work, not a foreground task', async () => {
    const initialMetadata = metadata({ activeTaskGroupIds: ['group-a', 'group-b'] })
    const subject = harness(leaseFixture({
      live: live({ activity: 'background', initialMetadata }),
      metadata: initialMetadata,
    }).lease, { activeTaskGroups: ref(new Set(['stale'])) })

    await expect(subject.api.subscribeSession()).resolves.toEqual({
      authoritative: true,
      live: true,
      backgroundOnly: true,
    })
    expect([...subject.activeTaskGroups.value]).toEqual(['group-a', 'group-b'])
  })

  it('fences stale same-session lease results', async () => {
    const oldLive = deferred<SessionReadLive>()
    const oldLease = leaseFixture({ live: oldLive.promise }).lease
    const newMetadata = metadata({
      runStatus: 'running',
      activeTask: { taskId: 'task-new', status: 'running' },
    })
    const newLease = leaseFixture({
      live: live({ activity: 'foreground', initialMetadata: newMetadata }),
      metadata: newMetadata,
    }).lease
    const subject = harness(oldLease)

    const stale = subject.api.subscribeSession()
    await Promise.resolve()
    subject.setLease(newLease)
    const current = subject.api.subscribeSession()
    oldLive.resolve(live())

    await expect(stale).resolves.toMatchObject({
      authoritative: false,
      cancelled: true,
    })
    await expect(current).resolves.toMatchObject({
      authoritative: true,
      live: true,
    })
    expect(subject.runStatus.value.status).toBe('running')
  })

  it('reports only the current live failure to metadata ownership', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const oldLive = deferred<SessionReadLive>()
      const newLive = deferred<SessionReadLive>()
      const sessionKey = ref('agent:main:webchat:old')
      let generation = 0
      const onSessionMetadataError = vi.fn()
      const subject = harness(leaseFixture({ live: oldLive.promise }).lease, {
        sessionKey,
        beginSessionMetadataResolution: () => ++generation,
        onSessionMetadataError,
      })

      const stale = subject.api.subscribeSession()
      await Promise.resolve()
      sessionKey.value = 'agent:main:webchat:new'
      subject.setLease(leaseFixture({ live: newLive.promise }).lease)
      const current = subject.api.subscribeSession()
      oldLive.reject(new Error('stale failure'))
      newLive.reject(new Error('current failure'))
      await Promise.all([stale, current])

      expect(onSessionMetadataError).toHaveBeenCalledOnce()
      expect(onSessionMetadataError).toHaveBeenCalledWith(
        'agent:main:webchat:new',
        2,
      )
    } finally {
      warn.mockRestore()
    }
  })

  it('retries metadata on the same lease without closing it', async () => {
    const recovered = metadata({ workspaceId: 'project-recovered' })
    const fixture = leaseFixture({ retryMetadata: async () => recovered })
    const onSessionMetadata = vi.fn()
    const subject = harness(fixture.lease, {
      beginSessionMetadataResolution: () => 12,
      onSessionMetadata,
    })

    await expect(subject.api.retrySessionMetadata()).resolves.toBe(true)
    expect(fixture.retryMetadata).toHaveBeenCalledOnce()
    expect(fixture.close).not.toHaveBeenCalled()
    expect(onSessionMetadata).toHaveBeenCalledWith(KEY, 12, recovered)
  })

  it('cancels and bounds metadata retry locally', async () => {
    vi.useFakeTimers()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const pendingRetry = deferred<SessionReadMetadata>()
      const fixture = leaseFixture({ retryMetadata: () => pendingRetry.promise })
      const onSessionMetadataError = vi.fn()
      const subject = harness(fixture.lease, {
        beginSessionMetadataResolution: () => 3,
        onSessionMetadataError,
      })
      const controller = new AbortController()
      const cancelled = subject.api.retrySessionMetadata({
        signal: controller.signal,
        timeoutMs: 50,
      })
      controller.abort()
      await expect(cancelled).resolves.toBe(false)
      expect(onSessionMetadataError).not.toHaveBeenCalled()

      const timedOut = subject.api.retrySessionMetadata({ timeoutMs: 50 })
      await vi.advanceTimersByTimeAsync(50)
      await expect(timedOut).resolves.toBe(false)
      expect(onSessionMetadataError).toHaveBeenCalledWith(KEY, 3)
    } finally {
      warn.mockRestore()
      vi.useRealTimers()
    }
  })

  it('reloads history and resets local live state only for generation change', async () => {
    const lastStreamSeq = ref(900)
    const generationChange = harness(leaseFixture({
      live: live({ reloadRequired: 'generationChanged' }),
    }).lease, { lastStreamSeq })
    await generationChange.api.subscribeSession()
    expect(lastStreamSeq.value).toBe(0)
    expect(generationChange.resetStreamLiveTurnState).toHaveBeenCalledOnce()
    expect(generationChange.loadHistory).toHaveBeenCalledOnce()

    const replaySeq = ref(42)
    const replayGap = harness(leaseFixture({
      live: live({ reloadRequired: 'replayGap' }),
    }).lease, { lastStreamSeq: replaySeq })
    await replayGap.api.subscribeSession()
    expect(replaySeq.value).toBe(42)
    expect(replayGap.resetStreamLiveTurnState).not.toHaveBeenCalled()
    expect(replayGap.loadHistory).toHaveBeenCalledOnce()
  })
})
