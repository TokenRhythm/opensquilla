// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'

import { RpcClient, type RpcCallOptions } from '@/lib/rpc'
import type {
  ChatMessage,
  ChatPendingItem,
  ChatRunStatus,
  ChatRunStatusSource,
} from '@/types/chat'
import type {
  SessionMessagesSnapshotResponse,
  SessionMessagesSubscribeResponse,
} from '@/types/rpc'
import { useChatRpcEventHandlers, type ChatRpcStreamApi } from './useChatRpcEventHandlers'
import { useChatRpcSubscriptions } from './useChatRpcSubscriptions'
import { useChatSessionBootstrap } from './useChatSessionBootstrap'
import { useChatSessionRuntime } from './useChatSessionRuntime'
import {
  useChatSessionSubscription,
  type UseChatSessionSubscriptionOptions,
} from './useChatSessionSubscription'

const SESSION_A = 'agent:main:webchat:race-a'
const SESSION_B = 'agent:main:webchat:race-b'

class SessionSwitchRaceSocket {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: SessionSwitchRaceSocket[] = []

  readonly sent: string[] = []
  readyState = SessionSwitchRaceSocket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    SessionSwitchRaceSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close(code = 1000, reason = '') {
    this.serverClose(code, reason)
  }

  serverClose(code = 1006, reason = 'server transport closed') {
    if (this.readyState === SessionSwitchRaceSocket.CLOSED) return
    this.readyState = SessionSwitchRaceSocket.CLOSED
    this.onclose?.({ code, reason, wasClean: code === 1000 } as CloseEvent)
  }

  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent)
  }
}

type RequestFrame = {
  type: 'req'
  id: string
  method: string
  params: Record<string, unknown>
}

function wireRequests(socket: SessionSwitchRaceSocket): RequestFrame[] {
  return socket.sent
    .map(frame => JSON.parse(frame) as RequestFrame)
    .filter(frame => frame.method !== 'connect')
}

function replyOk(
  socket: SessionSwitchRaceSocket,
  request: RequestFrame,
  payload: Record<string, unknown>,
) {
  socket.receive({ type: 'res', id: request.id, ok: true, payload })
}

function replyError(
  socket: SessionSwitchRaceSocket,
  request: RequestFrame,
  message = 'late source request failed',
) {
  socket.receive({
    type: 'res',
    id: request.id,
    ok: false,
    error: { code: 'SOURCE_REQUEST_FAILED', message },
  })
}

function connectSocket(socket: SessionSwitchRaceSocket, connId: string) {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    protocol: 3,
    policy: {
      tick_interval_ms: 30_000,
      concurrent_optional_read_methods: ['sessions.messages.hydrate'],
    },
    server: { conn_id: connId },
  })
}

type PendingQueueSwitch = (
  targetSessionKey: string,
  shouldCommit?: () => boolean,
  handoffSignal?: AbortSignal,
) => void | Promise<void>

function createSessionSwitchRaceHarness(options: {
  switchPendingQueue?: PendingQueueSwitch
} = {}) {
  const rpc = new RpcClient()
  rpc.connect('ws://session-switch-races.test')
  const socket = SessionSwitchRaceSocket.instances[0]!
  connectSocket(socket, 'conn-race-original')

  const sessionKey = ref(SESSION_A)
  const messages = ref<ChatMessage[]>([])
  const currentEpoch = ref(0)
  const lastStreamSeq = ref(0)
  const activeTaskGroups = ref(new Set<string>())
  const activeStreamTaskId = ref('')
  const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
  const snapshotCommits: Array<{ kind: string, key: string, marker?: unknown }> = []
  const metadataCommits: Array<{ key: string, marker?: unknown }> = []
  const metadataErrors: string[] = []
  const historyCommits: Array<{ key: string, marker?: unknown }> = []

  const sessionRunStatus = (
    source: ChatRunStatusSource | null | undefined,
  ): ChatRunStatus => ({
    status: source?.run_status === 'running' ? 'running' : 'idle',
    label: source?.run_status === 'running' ? 'Running' : '',
    task: null,
  })
  const subscriptionRpc: UseChatSessionSubscriptionOptions['rpc'] = {
    get connectionGeneration() { return rpc.connectionGeneration },
    get policy() { return rpc.policy },
    waitForConnection: (timeoutMs, signal, actions) => (
      rpc.waitForConnection(timeoutMs, signal, actions)
    ),
    call: <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      callOptions?: RpcCallOptions,
    ) => rpc.call(method, params, callOptions) as Promise<T>,
    recoverConnectionGeneration: (generation, reason) => (
      rpc.recoverConnectionGeneration(generation, reason)
    ),
  }
  const subscription = useChatSessionSubscription({
    rpc: subscriptionRpc,
    sessionKey,
    lastStreamSeq,
    runStatus,
    isStreaming: ref(false),
    hasActiveInterrupt: ref(false),
    activeStreamTaskId,
    activeTaskGroups,
    sessionRunStatus,
    startStreaming: vi.fn(),
    loadHistory: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    resetStreamLiveTurnState: vi.fn(),
    onLiveSnapshot: (snapshot: SessionMessagesSnapshotResponse) => {
      snapshotCommits.push({
        kind: 'snapshot',
        key: snapshot.key,
        marker: (snapshot as { marker?: unknown }).marker,
      })
    },
    onSnapshot: (snapshot: SessionMessagesSubscribeResponse) => {
      snapshotCommits.push({
        kind: 'subscription',
        key: (snapshot as { key?: string }).key ?? sessionKey.value,
        marker: (snapshot as { marker?: unknown }).marker,
      })
    },
    onSessionMetadata: (key, _generation, metadata) => {
      metadataCommits.push({ key, marker: metadata.workspaceId })
    },
    onSessionMetadataError: key => { metadataErrors.push(key) },
    beginSessionMetadataResolution: vi.fn(() => 1),
  })
  const bootstrap = useChatSessionBootstrap({
    sessionKey,
    loadHistory: async context => {
      const response = await rpc.call(
        'chat.history',
        { key: context.key },
        {
          timeoutMs: Math.max(1, context.attemptDeadlineAt - Date.now()),
          signal: context.signal,
          timeoutAction: 'reject',
          abortAction: 'reject',
          onSent: generation => context.markHistoryRequestSent?.(generation),
        },
      ) as { marker?: unknown }
      if (context.signal.aborted || context.key !== sessionKey.value) {
        return { ok: false, cancelled: true }
      }
      historyCommits.push({ key: context.key, marker: response.marker })
      return { ok: true }
    },
    subscribeSession: subscription.subscribeSession,
    cancelHistory: vi.fn(),
    cancelSubscription: subscription.cancelActiveSubscription,
    unsubscribeSession: subscription.unsubscribeSession,
  })

  const pendingQueue = ref<ChatPendingItem[]>([])
  const appendedText = vi.fn()
  const stream: ChatRpcStreamApi = {
    isStreaming: ref(false),
    streamBubble: ref(false),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    endStreaming: vi.fn(),
    checkpointForUserMessage: vi.fn(),
    acknowledgeSteerBoundary: vi.fn(),
    appendDelta: appendedText,
    scheduleRender: vi.fn(),
    appendToolCall: vi.fn(),
    appendToolDelta: vi.fn(),
    appendToolEnd: vi.fn(),
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetLiveTurnState: vi.fn(),
    resetAnswerGeneration: vi.fn(),
    setAssistantMessageId: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    setAcceptedActivityOrder: vi.fn(),
    setAcceptedActivityStartedAt: vi.fn(),
    recordCompactionActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
    useReducer: ref(false),
  }
  const eventScope = effectScope()
  const eventHandlers = eventScope.run(() => useChatRpcEventHandlers({
    sessionKey,
    currentEpoch,
    lastStreamSeq,
    activeTaskGroups,
    activeStreamTaskId,
    aborted: ref(false),
    messages,
    pendingQueue,
    usageAccum: ref({
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      cost: null,
      routedTurns: 0,
      sessionSaved: 0,
    }),
    usageModel: ref(''),
    stream,
    normalizeRunStatus: status => status,
    sessionRunStatus,
    applySessionRunState: subscription.applySessionRunState,
    queueRouterDecision: vi.fn(),
    bindRouterDecisionToModelCall: vi.fn(),
    appendEnsembleProgress: vi.fn(),
    markEnsembleHandoff: vi.fn(),
    flushPendingRouterDecision: vi.fn(),
    clearPendingRouterDecision: vi.fn(),
    handleRouterControlReplay: vi.fn(),
    showCompactionToast: vi.fn(),
    showWarningToast: vi.fn(),
    scheduleHistorySync: vi.fn(),
    schedulePendingDrainAfterTerminal: vi.fn(),
    popAllPendingIntoComposer: vi.fn(() => false),
    saveWidgetState: vi.fn(),
    handleSessionConnectionState: bootstrap.handleConnectionState,
    loadCurrentSessionUsage: vi.fn(),
    refreshRunModePreference: vi.fn(),
  }))!
  const rpcSubscriptions = useChatRpcSubscriptions(rpc, eventHandlers.handlers)
  const stopRpcSubscriptions = rpcSubscriptions.subscribe()

  const runtime = useChatSessionRuntime({
    sessionKey,
    messages,
    pendingSessionIntent: ref(null),
    routerDecisionPending: ref(null),
    currentEpoch,
    lastStreamSeq,
    activeTaskGroups,
    activeStreamTaskId,
    aborted: ref(false),
    lastHeaderRole: ref(''),
    lastHeaderDay: ref(''),
    usageAccum: ref({
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      cost: null,
      routedTurns: 0,
      sessionSaved: 0,
    }),
    usageModel: ref(''),
    createSessionKey: () => '',
    persistSession: key => { sessionKey.value = key },
    cancelSessionBootstrap: bootstrap.cancelSessionBootstrap,
    setSessionHandoffTarget: bootstrap.setSessionHandoffTarget,
    resumeSessionBootstrap: vi.fn(),
    startSessionBootstrap: bootstrap.startSessionBootstrap,
    loadCurrentSessionUsage: vi.fn(),
    applySessionRunState: subscription.applySessionRunState,
    setCompactInFlight: vi.fn(),
    hideCompactStatus: vi.fn(),
    clearPendingQueue: vi.fn(),
    switchPendingQueue: options.switchPendingQueue ?? vi.fn(),
    adoptPendingQueue: vi.fn(),
    resetSavingsPopupCooldown: vi.fn(),
    restoreWidgetState: vi.fn(),
    resetStreamLiveTurnState: vi.fn(),
  })

  const startInitialSession = async (hydrationComplete = true) => {
    const run = bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => {
      expect(wireRequests(socket).filter(frame => (
        frame.params.key === SESSION_A
        && ['sessions.messages.subscribe', 'sessions.messages.snapshot'].includes(frame.method)
      ))).toHaveLength(2)
    })
    const subscribe = wireRequests(socket).find(frame => (
      frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_A
    ))!
    const snapshot = wireRequests(socket).find(frame => (
      frame.method === 'sessions.messages.snapshot' && frame.params.key === SESSION_A
    ))!
    replyOk(socket, subscribe, {
      key: SESSION_A,
      subscribed: true,
      hydration_complete: hydrationComplete,
      run_status: 'idle',
      current_stream_seq: 0,
      replay_complete: true,
      stream_generation: 'stream-a',
      workspaceId: 'workspace-a',
      marker: 'a-subscribe',
    })
    replyOk(socket, snapshot, {
      key: SESSION_A,
      events: [],
      current_stream_seq: 0,
      stream_generation: 'stream-a',
      marker: 'a-snapshot',
    })
    await run.live
    return run
  }

  const replyToTargetBootstrap = async (targetSocket = socket) => {
    await vi.waitFor(() => {
      expect(wireRequests(targetSocket).some(frame => (
        frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_B
      ))).toBe(true)
      expect(wireRequests(targetSocket).some(frame => (
        frame.method === 'sessions.messages.snapshot' && frame.params.key === SESSION_B
      ))).toBe(true)
      expect(wireRequests(targetSocket).some(frame => (
        frame.method === 'chat.history' && frame.params.key === SESSION_B
      ))).toBe(true)
    })
    const targetFrames = wireRequests(targetSocket).filter(frame => frame.params.key === SESSION_B)
    const subscribe = targetFrames.find(frame => frame.method === 'sessions.messages.subscribe')!
    const snapshot = targetFrames.find(frame => frame.method === 'sessions.messages.snapshot')!
    const history = targetFrames.find(frame => frame.method === 'chat.history')!
    replyOk(targetSocket, subscribe, {
      key: SESSION_B,
      subscribed: true,
      hydration_complete: true,
      run_status: 'idle',
      current_stream_seq: 0,
      replay_complete: true,
      stream_generation: 'stream-b',
      workspaceId: 'workspace-b',
      marker: 'b-subscribe',
    })
    replyOk(targetSocket, snapshot, {
      key: SESSION_B,
      events: [],
      current_stream_seq: 0,
      stream_generation: 'stream-b',
      marker: 'b-snapshot',
    })
    replyOk(targetSocket, history, { marker: 'b-history' })
    const sourceRelease = wireRequests(targetSocket).find(frame => (
      frame.method === 'sessions.messages.unsubscribe' && frame.params.key === SESSION_A
    ))
    if (sourceRelease) replyOk(targetSocket, sourceRelease, { unsubscribed: true })
  }

  return {
    rpc,
    socket,
    sessionKey,
    lastStreamSeq,
    snapshotCommits,
    metadataCommits,
    metadataErrors,
    historyCommits,
    appendedText,
    subscription,
    bootstrap,
    runtime,
    startInitialSession,
    replyToTargetBootstrap,
    dispose: () => {
      stopRpcSubscriptions()
      eventScope.stop()
      rpc.disconnect()
    },
  }
}

describe('session switch transport ownership race matrix', () => {
  let dispose: (() => void) | null = null

  beforeEach(() => {
    SessionSwitchRaceSocket.instances = []
    localStorage.clear()
    vi.stubGlobal('WebSocket', SessionSwitchRaceSocket)
  })

  afterEach(() => {
    dispose?.()
    dispose = null
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('defers a connected winner until the pending queue commits without restarting A', async () => {
    let releaseQueue!: () => void
    const queue = new Promise<void>(resolve => { releaseQueue = resolve })
    const harness = createSessionSwitchRaceHarness({
      switchPendingQueue: () => queue,
    })
    dispose = harness.dispose
    await harness.startInitialSession()
    const generation = harness.rpc.connectionGeneration
    const baseline = wireRequests(harness.socket).length

    const switching = harness.runtime.switchToSession(SESSION_B)
    const connectedWinner = harness.bootstrap.handleConnectionState('connected')
    expect(connectedWinner?.deferred).toBe(true)
    expect(harness.sessionKey.value).toBe(SESSION_A)
    expect(wireRequests(harness.socket)).toHaveLength(baseline)

    releaseQueue()
    await harness.replyToTargetBootstrap()
    await switching

    const postBaseline = wireRequests(harness.socket).slice(baseline)
    expect(postBaseline.filter(frame => frame.params.key === SESSION_A).map(frame => frame.method))
      .toEqual(['sessions.messages.unsubscribe'])
    expect(postBaseline.filter(frame => frame.params.key === SESSION_B).map(frame => frame.method))
      .toEqual([
        'sessions.messages.subscribe',
        'sessions.messages.snapshot',
        'chat.history',
      ])
    expect(SessionSwitchRaceSocket.instances).toHaveLength(1)
    expect(harness.rpc.connectionGeneration).toBe(generation)
    expect(harness.sessionKey.value).toBe(SESSION_B)
  })

  it('rolls a failed pending-queue handoff back to A after connected wins', async () => {
    let rejectQueue!: (error: Error) => void
    const queue = new Promise<void>((_resolve, reject) => { rejectQueue = reject })
    const harness = createSessionSwitchRaceHarness({
      switchPendingQueue: () => queue,
    })
    dispose = harness.dispose
    await harness.startInitialSession()
    const generation = harness.rpc.connectionGeneration
    const baseline = wireRequests(harness.socket).length
    const failure = new Error('synthetic pending queue failure')

    const switching = harness.runtime.switchToSession(SESSION_B)
    expect(harness.bootstrap.handleConnectionState('connected')?.deferred).toBe(true)
    rejectQueue(failure)
    await expect(switching).rejects.toBe(failure)

    expect(harness.sessionKey.value).toBe(SESSION_A)
    expect(wireRequests(harness.socket)).toHaveLength(baseline)
    expect(SessionSwitchRaceSocket.instances).toHaveLength(1)
    expect(harness.rpc.connectionGeneration).toBe(generation)
    expect(harness.rpc.state).toBe('connected')
  })

  it('drops late A ACK, snapshot, hydrate, and event after B becomes current', async () => {
    const harness = createSessionSwitchRaceHarness()
    dispose = harness.dispose
    await harness.startInitialSession(false)
    await vi.waitFor(() => {
      expect(wireRequests(harness.socket).some(frame => (
        frame.method === 'sessions.messages.hydrate' && frame.params.key === SESSION_A
      ))).toBe(true)
    })
    const staleHydrate = wireRequests(harness.socket).find(frame => (
      frame.method === 'sessions.messages.hydrate' && frame.params.key === SESSION_A
    ))!

    harness.snapshotCommits.length = 0
    harness.metadataCommits.length = 0
    harness.historyCommits.length = 0
    const staleRun = harness.bootstrap.startSessionBootstrap({
      includeHistory: false,
      force: true,
    })
    await vi.waitFor(() => {
      expect(wireRequests(harness.socket).filter(frame => (
        frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_A
      ))).toHaveLength(2)
      expect(wireRequests(harness.socket).filter(frame => (
        frame.method === 'sessions.messages.snapshot' && frame.params.key === SESSION_A
      ))).toHaveLength(2)
    })
    const staleSubscribes = wireRequests(harness.socket).filter(frame => (
      frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_A
    ))
    const staleSnapshots = wireRequests(harness.socket).filter(frame => (
      frame.method === 'sessions.messages.snapshot' && frame.params.key === SESSION_A
    ))
    const staleSubscribe = staleSubscribes[staleSubscribes.length - 1]!
    const staleSnapshot = staleSnapshots[staleSnapshots.length - 1]!

    const switching = harness.runtime.switchToSession(SESSION_B)
    await harness.replyToTargetBootstrap()
    await switching
    const targetCommitCount = harness.snapshotCommits.length

    replyOk(harness.socket, staleSubscribe, {
      key: SESSION_A,
      subscribed: true,
      hydration_complete: true,
      run_status: 'running',
      current_stream_seq: 50,
      replay_complete: true,
      stream_generation: 'stream-a-late',
      workspaceId: 'workspace-a-late',
      marker: 'late-a-subscribe',
    })
    replyOk(harness.socket, staleSnapshot, {
      key: SESSION_A,
      events: [{ event: 'late-a-snapshot' }],
      current_stream_seq: 50,
      stream_generation: 'stream-a-late',
      marker: 'late-a-snapshot',
    })
    replyOk(harness.socket, staleHydrate, {
      key: SESSION_A,
      hydration_complete: true,
      run_status: 'running',
      current_stream_seq: 51,
      replay_complete: true,
      workspaceId: 'workspace-a-late-hydrate',
      marker: 'late-a-hydrate',
    })
    harness.socket.receive({
      type: 'event',
      event: 'session.event.text_delta',
      payload: {
        session_key: SESSION_A,
        turn_id: 'turn-a-late',
        stream_seq: 99,
        generation_epoch: 0,
        text: 'stale A output',
      },
    })
    harness.socket.receive({
      type: 'event',
      event: 'session.event.text_delta',
      payload: {
        session_key: SESSION_B,
        turn_id: 'turn-b-current',
        stream_seq: 1,
        generation_epoch: 0,
        text: 'current B output',
      },
    })
    await staleRun.live
    await Promise.resolve()

    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(harness.snapshotCommits).toHaveLength(targetCommitCount)
    expect(harness.snapshotCommits.every(commit => commit.key === SESSION_B)).toBe(true)
    expect(harness.metadataCommits.every(commit => commit.key === SESSION_B)).toBe(true)
    expect(harness.historyCommits).toEqual([{ key: SESSION_B, marker: 'b-history' }])
    expect(harness.metadataErrors).toEqual([])
    expect(harness.appendedText).toHaveBeenCalledTimes(1)
    expect(harness.appendedText).toHaveBeenCalledWith('current B output')
    expect(harness.lastStreamSeq.value).toBe(1)
    expect(SessionSwitchRaceSocket.instances).toHaveLength(1)
  })

  it('drops a late A subscribe error and its paired snapshot after B is live', async () => {
    const harness = createSessionSwitchRaceHarness()
    dispose = harness.dispose
    await harness.startInitialSession()
    harness.snapshotCommits.length = 0
    harness.metadataCommits.length = 0

    const staleRun = harness.bootstrap.startSessionBootstrap({
      includeHistory: false,
      force: true,
    })
    await vi.waitFor(() => {
      expect(wireRequests(harness.socket).filter(frame => (
        frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_A
      ))).toHaveLength(2)
    })
    const staleSubscribes = wireRequests(harness.socket).filter(frame => (
      frame.method === 'sessions.messages.subscribe' && frame.params.key === SESSION_A
    ))
    const staleSnapshots = wireRequests(harness.socket).filter(frame => (
      frame.method === 'sessions.messages.snapshot' && frame.params.key === SESSION_A
    ))
    const staleSubscribe = staleSubscribes[staleSubscribes.length - 1]!
    const staleSnapshot = staleSnapshots[staleSnapshots.length - 1]!

    const switching = harness.runtime.switchToSession(SESSION_B)
    await harness.replyToTargetBootstrap()
    await switching
    const targetCommitCount = harness.snapshotCommits.length

    replyError(harness.socket, staleSubscribe)
    replyOk(harness.socket, staleSnapshot, {
      key: SESSION_A,
      events: [{ event: 'late-error-paired-snapshot' }],
      current_stream_seq: 80,
      stream_generation: 'stream-a-error',
      marker: 'late-a-error-snapshot',
    })
    await staleRun.live

    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(harness.snapshotCommits).toHaveLength(targetCommitCount)
    expect(harness.snapshotCommits.every(commit => commit.key === SESSION_B)).toBe(true)
    expect(harness.metadataCommits.every(commit => commit.key === SESSION_B)).toBe(true)
    expect(harness.metadataErrors).toEqual([])
    expect(SessionSwitchRaceSocket.instances).toHaveLength(1)
    expect(harness.rpc.state).toBe('connected')
  })

  it('binds only B when the real socket closes inside a delayed handoff', async () => {
    let releaseQueue!: () => void
    const queue = new Promise<void>(resolve => { releaseQueue = resolve })
    const harness = createSessionSwitchRaceHarness({
      switchPendingQueue: () => queue,
    })
    dispose = harness.dispose
    await harness.startInitialSession()
    const originalGeneration = harness.rpc.connectionGeneration

    const switching = harness.runtime.switchToSession(SESSION_B)
    harness.socket.serverClose(1006, 'handoff transport loss')
    expect(harness.rpc.state).toBe('disconnected')

    harness.rpc.connect('ws://session-switch-races.test')
    const replacement = SessionSwitchRaceSocket.instances[1]!
    connectSocket(replacement, 'conn-race-replacement')
    expect(harness.rpc.state).toBe('connected')
    expect(wireRequests(replacement)).toEqual([])

    releaseQueue()
    await harness.replyToTargetBootstrap(replacement)
    await switching

    expect(wireRequests(replacement).filter(frame => frame.params.key === SESSION_A))
      .toEqual([])
    expect(wireRequests(replacement).filter(frame => frame.params.key === SESSION_B).map(frame => frame.method))
      .toEqual([
        'sessions.messages.subscribe',
        'sessions.messages.snapshot',
        'chat.history',
      ])
    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(harness.rpc.connectionGeneration).not.toBe(originalGeneration)
    expect(SessionSwitchRaceSocket.instances).toHaveLength(2)
    expect(replacement.readyState).toBe(SessionSwitchRaceSocket.OPEN)
  })
})
