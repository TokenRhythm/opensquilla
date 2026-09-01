// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import {
  RpcAbortError,
  RpcClient,
  type RpcCallOptions,
} from '@/lib/rpc'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import { acceptStreamSeq } from '@/utils/chat/streamEvents'
import { useChatSessionBootstrap } from './useChatSessionBootstrap'
import { useChatSessionRuntime } from './useChatSessionRuntime'
import {
  useChatSessionSubscription as useProductionChatSessionSubscription,
} from './useChatSessionSubscription'
import type { SessionBootstrapPhaseContext } from './sessionBootstrapContract'
import { createV4SessionReadPort } from '@/adapters/gateway/sessionReadPortV4'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import { createConversationSubscriptionLifecycle } from '@/modules/conversationSubscriptionLifecycle'
import {
  createSessionReadLifecycle,
  type SessionReadPortLease,
} from '@/modules/sessionReadLifecycle'

const SESSION_A = 'agent:main:webchat:a'
const SESSION_B = 'agent:main:webchat:b'

class SessionSwitchSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: SessionSwitchSocket[] = []

  readonly sent: string[] = []
  readyState = SessionSwitchSocket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    SessionSwitchSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close(code = 1000, reason = '') {
    if (this.readyState === SessionSwitchSocket.CLOSED) return
    this.readyState = SessionSwitchSocket.CLOSED
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

type RpcInternals = {
  _pending: Map<string, unknown>
  _listeners: Map<string, Set<unknown>>
  _reconnectAttempt: number
}

type QueueSwitch = (
  targetSessionKey: string,
  shouldCommit?: () => boolean,
  handoffSignal?: AbortSignal,
) => void | Promise<void>

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function requests(socket: SessionSwitchSocket): RequestFrame[] {
  return socket.sent.flatMap(frame => {
    const parsed = JSON.parse(frame) as Partial<RequestFrame>
    return parsed.type === 'req' && parsed.method && parsed.method !== 'connect'
      ? [parsed as RequestFrame]
      : []
  })
}

function keyedRequests(
  socket: SessionSwitchSocket,
  method?: string,
  key?: string,
): RequestFrame[] {
  return requests(socket).filter(frame => (
    (!method || frame.method === method)
    && (!key || frame.params.key === key)
  ))
}

function reply(
  socket: SessionSwitchSocket,
  request: RequestFrame,
  payload: Record<string, unknown>,
) {
  socket.receive({ type: 'res', id: request.id, ok: true, payload })
}

function reject(
  socket: SessionSwitchSocket,
  request: RequestFrame,
  message: string,
) {
  socket.receive({
    type: 'res',
    id: request.id,
    ok: false,
    error: { code: 'TEST_FAILURE', message },
  })
}

function subscribePayload(
  key: string,
  marker = key,
  overrides: Record<string, unknown> = {},
) {
  return {
    workspaceId: null,
    projectWorkspace: { marker },
    projectWorkspaceDeferred: false,
    active_task_group_ids: [],
    run_mode_lock: { locked: false },
    pendingUserInputs: [],
    collaboration: null,
    routing: null,
    currentPlan: null,
    activePlanRun: null,
    goal: null,
    goalSnapshotStreamSeq: null,
    tasks: [],
    active_task: null,
    last_task: null,
    queued_task_ids: [],
    epoch: null,
    deferred_fields: [],
    key,
    marker,
    subscribed: true,
    hydration_complete: true,
    run_status: 'idle',
    stream_generation: 'test-stream-generation',
    current_stream_seq: 0,
    replay_complete: true,
    replay_gap_reason: null,
    replayed_count: 0,
    ...overrides,
  }
}

function snapshotPayload(key: string, marker = key) {
  return {
    key,
    marker,
    task_id: null,
    stream_generation: 'test-stream-generation',
    events: [{
      event: 'session.event.state_changed',
      payload: { marker },
    }],
    current_stream_seq: 0,
  }
}

function historyPayload() {
  return {
    messages: [],
    has_more: false,
    oldest_cursor: null,
    newest_cursor: null,
    history_scope: 'complete',
    loaded_count: 0,
    page_size: 100,
    canonical_available: true,
    canonical_complete: true,
    compaction_summaries: [],
    turn_outcomes: [],
  }
}

function authenticate(
  socket: SessionSwitchSocket,
  connId = 'conn-stable',
  policy: Record<string, unknown> = {},
) {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    protocol: 3,
    policy: { tick_interval_ms: 30_000, ...policy },
    server: { conn_id: connId },
  })
}

async function flushMicrotasks(turns = 8) {
  for (let index = 0; index < turns; index += 1) await Promise.resolve()
}

function rpcInternals(client: RpcClient): RpcInternals {
  return client as unknown as RpcInternals
}

function listenerCount(client: RpcClient): number {
  let count = 0
  for (const listeners of rpcInternals(client)._listeners.values()) {
    count += listeners.size
  }
  return count
}

function createHarness(options: {
  connected?: boolean
  connId?: string
  policy?: Record<string, unknown>
  snapshots?: boolean
  switchPendingQueue?: QueueSwitch
} = {}) {
  const rpc = new RpcClient()
  const stateTrace: string[] = []
  const transportTrace: Array<Record<string, unknown>> = []
  const offStateTrace = rpc.on('_state', state => {
    stateTrace.push(String(state))
  })
  const offTransportTrace = rpc.on('_transport', detail => {
    transportTrace.push(detail as Record<string, unknown>)
  })
  rpc.connect('ws://session-switch.test')
  const socket = SessionSwitchSocket.instances[0]!
  if (options.connected !== false) {
    authenticate(socket, options.connId, options.policy)
  }

  const sessionKey = ref(SESSION_A)
  const messages = ref<ChatMessage[]>([])
  const lastStreamSeq = ref(0)
  const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
  const appliedSnapshots: Array<Record<string, unknown>> = []
  const liveSnapshots: Array<Record<string, unknown>> = []
  const metadata: Array<{ key: string, marker: unknown }> = []
  const metadataErrors: string[] = []
  let metadataGeneration = 0

  const conversationRuntime = createConversationRuntime()
  const sessionReadLifecycle = createSessionReadLifecycle({
    port: createV4SessionReadPort({
      request: <T = unknown>(method: string, params?: Record<string, unknown>, callOptions?: RpcCallOptions) => {
        const key = String(params?.key ?? params?.sessionKey ?? sessionKey.value)
        if (method === 'sessions.messages.snapshot' && !options.snapshots) {
          callOptions?.onSent?.(rpc.connectionGeneration)
          return Promise.resolve(snapshotPayload(key) as T)
        }
        if (method === 'chat.history') {
          callOptions?.onSent?.(rpc.connectionGeneration)
          return Promise.resolve(historyPayload() as T)
        }
        return rpc.call(method, params, callOptions) as Promise<T>
      },
      ready: readyOptions => rpc.ready(
        readyOptions?.timeoutMs,
        readyOptions?.signal,
        {
          timeoutAction: readyOptions?.timeoutAction,
          abortAction: readyOptions?.abortAction,
        },
      ),
      get generation() { return rpc.connectionGeneration },
    }, {
      concurrentHistoryReads: () => {
        const methods = rpc.policy?.concurrent_optional_read_methods
        return Array.isArray(methods) && methods.includes('chat.history')
      },
    }),
    runtime: conversationRuntime,
    subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
  })

  const subscription = useProductionChatSessionSubscription({
    sessionReadLeaseReader: sessionReadLifecycle,
    conversationRuntime,
    sessionKey,
    lastStreamSeq,
    runStatus,
    isStreaming: ref(false),
    hasActiveInterrupt: ref(false),
    activeStreamTaskId: ref(''),
    activeTaskGroups: ref(new Set<string>()),
    sessionRunStatus: () => ({ status: 'idle', label: '', task: null }),
    startStreaming: vi.fn(),
    loadHistory: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    resetStreamLiveTurnState: vi.fn(),
    beginSessionMetadataResolution: () => ++metadataGeneration,
    onSessionMetadata: (key, _generation, payload) => {
      metadata.push({
        key,
        marker: (payload.projectWorkspace as Record<string, unknown> | null)?.marker,
      })
    },
    onSessionMetadataError: key => { metadataErrors.push(key) },
    onSnapshot: payload => {
      appliedSnapshots.push({
        ...(payload as unknown as Record<string, unknown>),
        marker: payload.additional.marker,
      })
    },
    ...(options.snapshots
      ? {
          onLiveSnapshot: payload => {
            liveSnapshots.push({
              ...(payload as unknown as Record<string, unknown>),
              marker: payload.events[0]?.payload.marker,
            })
          },
        }
      : {}),
  })

  const loadHistory = vi.fn(async (context: SessionBootstrapPhaseContext) => {
    const lease = sessionReadLifecycle.current()
    if (!lease) return { ok: false, error: new Error('Session read lease is unavailable') }
    await lease.history.latest({
      signal: context.signal,
      deadlineAt: context.attemptDeadlineAt,
      budgetMs: Math.max(1, context.attemptDeadlineAt - Date.now()),
    })
    return { ok: true }
  })
  const bootstrap = useChatSessionBootstrap({
    sessionKey,
    sessionReadLifecycle,
    loadHistory,
    subscribeSession: subscription.subscribeSession,
    cancelHistory: vi.fn(),
    cancelSubscription: subscription.cancelActiveSubscription,
  })

  const persistSession = vi.fn((key: string) => { sessionKey.value = key })
  const switchPendingQueue = vi.fn<QueueSwitch>(
    options.switchPendingQueue ?? (() => undefined),
  )
  const runtime = useChatSessionRuntime({
    sessionKey,
    messages,
    pendingSessionIntent: ref(null),
    routerDecisionPending: ref(null),
    currentEpoch: ref(0),
    lastStreamSeq,
    activeTaskGroups: ref(new Set<string>()),
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
    persistSession,
    cancelSessionBootstrap: bootstrap.cancelSessionBootstrap,
    setSessionHandoffTarget: bootstrap.setSessionHandoffTarget,
    startSessionBootstrap: bootstrap.startSessionBootstrap,
    loadCurrentSessionUsage: vi.fn(),
    applySessionRunState: subscription.applySessionRunState,
    setCompactInFlight: vi.fn(),
    hideCompactStatus: vi.fn(),
    clearPendingQueue: vi.fn(),
    switchPendingQueue,
    adoptPendingQueue: vi.fn(),
    resetSavingsPopupCooldown: vi.fn(),
    restoreWidgetState: vi.fn(),
    resetStreamLiveTurnState: vi.fn(),
  })

  return {
    rpc,
    socket,
    sessionKey,
    lastStreamSeq,
    stateTrace,
    transportTrace,
    subscription,
    bootstrap,
    runtime,
    persistSession,
    switchPendingQueue,
    appliedSnapshots,
    liveSnapshots,
    metadata,
    metadataErrors,
    attachConnectionBridge: () => rpc.on('_state', state => {
      bootstrap.handleConnectionState(String(state))
    }),
    dispose: () => {
      offStateTrace()
      offTransportTrace()
    },
  }
}

describe('session switch transport ownership', () => {
  let rpc: RpcClient | null = null

  beforeEach(() => {
    SessionSwitchSocket.instances = []
    localStorage.clear()
    vi.stubGlobal('WebSocket', SessionSwitchSocket)
  })

  afterEach(() => {
    rpc?.disconnect()
    rpc = null
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('abandons A in connection wait and subscribes only B on the same socket', async () => {
    const harness = createHarness({ connected: false })
    rpc = harness.rpc
    const generation = harness.rpc.connectionGeneration
    const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await flushMicrotasks()
    expect(requests(harness.socket)).toEqual([])

    harness.stateTrace.length = 0
    const switching = harness.runtime.switchToSession(SESSION_B)
    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(requests(harness.socket)).toEqual([])

    authenticate(harness.socket)
    await vi.waitFor(() => {
      expect(keyedRequests(
        harness.socket,
        'sessions.messages.subscribe',
        SESSION_B,
      )).toHaveLength(1)
    })
    const subscribeB = keyedRequests(
      harness.socket,
      'sessions.messages.subscribe',
      SESSION_B,
    )[0]!
    reply(harness.socket, subscribeB, subscribePayload(SESSION_B, 'B'))

    await switching
    await initial.live
    expect(keyedRequests(
      harness.socket,
      'sessions.messages.subscribe',
      SESSION_A,
    )).toEqual([])
    expect(harness.appliedSnapshots.map(value => value.marker)).toEqual(['B'])
    expect(SessionSwitchSocket.instances).toHaveLength(1)
    expect(harness.rpc.connectionGeneration).toBe(generation)
    expect(harness.stateTrace).not.toContain('disconnected')
  })

  it.each(['ack', 'error'] as const)(
    'keeps A subscribe %s local while releasing A and acquiring B in wire order',
    async oldResult => {
      vi.spyOn(console, 'warn').mockImplementation(() => {})
      const harness = createHarness()
      rpc = harness.rpc
      const generation = harness.rpc.connectionGeneration
      harness.bootstrap.startSessionBootstrap({ includeHistory: false })
      await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(1))

      harness.stateTrace.length = 0
      const switching = harness.runtime.switchToSession(SESSION_B)
      await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(3))
      const wire = requests(harness.socket)
      expect(wire.map(frame => [frame.method, frame.params.key])).toEqual([
        ['sessions.messages.subscribe', SESSION_A],
        ['sessions.messages.unsubscribe', SESSION_A],
        ['sessions.messages.subscribe', SESSION_B],
      ])

      if (oldResult === 'ack') {
        reply(harness.socket, wire[0]!, subscribePayload(SESSION_A, 'late-A'))
      } else {
        reject(harness.socket, wire[0]!, 'late A subscribe failed')
      }
      reply(harness.socket, wire[1]!, { unsubscribed: true })
      reply(harness.socket, wire[2]!, subscribePayload(SESSION_B, 'B'))
      await switching

      expect(harness.sessionKey.value).toBe(SESSION_B)
      expect(harness.appliedSnapshots.map(value => value.marker)).toEqual(['B'])
      expect(harness.metadataErrors).toEqual([])
      expect(SessionSwitchSocket.instances).toHaveLength(1)
      expect(harness.rpc.connectionGeneration).toBe(generation)
      expect(harness.stateTrace).not.toContain('disconnected')
      expect(harness.transportTrace.some(event => (
        event.phase === 'reconnect_scheduled'
      ))).toBe(false)
    },
  )

  it('drops A snapshot that settles after B has become authoritative', async () => {
    const harness = createHarness({ snapshots: true })
    rpc = harness.rpc
    harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(2))
    const initial = requests(harness.socket)
    expect(initial.map(frame => frame.method)).toEqual([
      'sessions.messages.subscribe',
      'sessions.messages.snapshot',
    ])
    reply(harness.socket, initial[0]!, subscribePayload(SESSION_A, 'A-ack'))

    const switching = harness.runtime.switchToSession(SESSION_B)
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(5))
    const wire = requests(harness.socket)
    expect(wire.map(frame => [frame.method, frame.params.key])).toEqual([
      ['sessions.messages.subscribe', SESSION_A],
      ['sessions.messages.snapshot', SESSION_A],
      ['sessions.messages.unsubscribe', SESSION_A],
      ['sessions.messages.subscribe', SESSION_B],
      ['sessions.messages.snapshot', SESSION_B],
    ])

    reply(harness.socket, wire[2]!, { unsubscribed: true })
    reply(harness.socket, wire[3]!, subscribePayload(SESSION_B, 'B-ack'))
    reply(harness.socket, wire[4]!, snapshotPayload(SESSION_B, 'B-snapshot'))
    await switching
    reply(harness.socket, wire[1]!, snapshotPayload(SESSION_A, 'late-A-snapshot'))
    await flushMicrotasks()

    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(harness.liveSnapshots.map(value => value.marker)).toEqual(['B-snapshot'])
    expect(harness.appliedSnapshots.map(value => value.marker)).toEqual(['B-ack'])
    expect(SessionSwitchSocket.instances).toHaveLength(1)
  })

  it('drops A hydrate and A events that arrive after B is current', async () => {
    const harness = createHarness({
      policy: {
        concurrent_optional_read_methods: ['sessions.messages.hydrate'],
      },
    })
    rpc = harness.rpc
    const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(1))
    const subscribeA = requests(harness.socket)[0]!
    reply(harness.socket, subscribeA, subscribePayload(
      SESSION_A,
      'A-fast-ack',
      { hydration_complete: false },
    ))
    await initial.live
    await vi.waitFor(() => {
      expect(keyedRequests(
        harness.socket,
        'sessions.messages.hydrate',
        SESSION_A,
      )).toHaveLength(1)
    })
    const hydrateA = keyedRequests(
      harness.socket,
      'sessions.messages.hydrate',
      SESSION_A,
    )[0]!

    const rendered: string[] = []
    const offEvent = harness.rpc.on('session.event.text_delta', payload => {
      const event = payload as {
        session_key?: string
        stream_seq?: number
        text?: string
      }
      const decision = acceptStreamSeq(
        event,
        harness.sessionKey.value,
        harness.lastStreamSeq.value,
      )
      if (!decision.accepted) return
      harness.lastStreamSeq.value = decision.nextStreamSeq
      if (event.text) rendered.push(event.text)
    })

    const switching = harness.runtime.switchToSession(SESSION_B)
    await vi.waitFor(() => {
      expect(keyedRequests(
        harness.socket,
        'sessions.messages.subscribe',
        SESSION_B,
      )).toHaveLength(1)
    })
    const unsubscribeA = keyedRequests(
      harness.socket,
      'sessions.messages.unsubscribe',
      SESSION_A,
    )[0]!
    const subscribeB = keyedRequests(
      harness.socket,
      'sessions.messages.subscribe',
      SESSION_B,
    )[0]!
    reply(harness.socket, unsubscribeA, { unsubscribed: true })
    reply(harness.socket, subscribeB, subscribePayload(SESSION_B, 'B'))
    await switching

    reply(harness.socket, hydrateA, {
      hydration_complete: true,
      projectWorkspace: { marker: 'late-A-hydrate' },
    })
    harness.socket.receive({
      type: 'event',
      event: 'session.event.text_delta',
      payload: { session_key: SESSION_A, stream_seq: 1, text: 'late A' },
    })
    harness.socket.receive({
      type: 'event',
      event: 'session.event.text_delta',
      payload: { session_key: SESSION_B, stream_seq: 1, text: 'live B' },
    })
    await flushMicrotasks()
    offEvent()

    expect(harness.metadata.map(value => value.key)).toEqual([SESSION_B])
    expect(harness.metadata.map(value => value.marker)).not.toContain('late-A-hydrate')
    expect(rendered).toEqual(['live B'])
    expect(harness.lastStreamSeq.value).toBe(1)
    expect(rpcInternals(harness.rpc)._pending.size).toBe(0)
  })

  it('creates A2 instead of reviving releasing A1 during A to B to A', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const harness = createHarness()
    rpc = harness.rpc
    harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(1))

    const toB = harness.runtime.switchToSession(SESSION_B)
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(3))
    const backToA = harness.runtime.switchToSession(SESSION_A)
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(5))
    const wire = requests(harness.socket)
    expect(wire.map(frame => [frame.method, frame.params.key])).toEqual([
      ['sessions.messages.subscribe', SESSION_A],
      ['sessions.messages.unsubscribe', SESSION_A],
      ['sessions.messages.subscribe', SESSION_B],
      ['sessions.messages.unsubscribe', SESSION_B],
      ['sessions.messages.subscribe', SESSION_A],
    ])

    reply(harness.socket, wire[1]!, { unsubscribed: true })
    reply(harness.socket, wire[3]!, { unsubscribed: true })
    reply(harness.socket, wire[4]!, subscribePayload(SESSION_A, 'A2'))
    reply(harness.socket, wire[0]!, subscribePayload(SESSION_A, 'late-A1'))
    reject(harness.socket, wire[2]!, 'late B subscribe failed')
    await Promise.all([toB, backToA])

    expect(harness.sessionKey.value).toBe(SESSION_A)
    expect(harness.appliedSnapshots.map(value => value.marker)).toEqual(['A2'])

    // The public lease behavior is the externally observable invariant: one
    // final A2 release is emitted, and a second release finds no reusable A1.
    const beforeRelease = requests(harness.socket).length
    harness.bootstrap.cancelSessionBootstrap()
    await vi.waitFor(() => {
      expect(requests(harness.socket)).toHaveLength(beforeRelease + 1)
    })
    const releaseFrame = requests(harness.socket)[beforeRelease]!
    expect([releaseFrame.method, releaseFrame.params.key]).toEqual([
      'sessions.messages.unsubscribe',
      SESSION_A,
    ])
    reply(harness.socket, releaseFrame, { unsubscribed: true })
    await flushMicrotasks()
    harness.bootstrap.cancelSessionBootstrap()
    expect(requests(harness.socket)).toHaveLength(beforeRelease + 1)
  })

  it('defers a connected race until the pending queue permits B to commit', async () => {
    const queue = deferred<void>()
    const harness = createHarness({
      switchPendingQueue: (_target, shouldCommit) => (
        queue.promise.then(() => { shouldCommit?.() })
      ),
    })
    rpc = harness.rpc
    const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(1))
    reply(harness.socket, requests(harness.socket)[0]!, subscribePayload(SESSION_A, 'A'))
    await initial.live
    const before = requests(harness.socket).length

    const switching = harness.runtime.switchToSession(SESSION_B)
    const deferredRun = harness.bootstrap.handleConnectionState('connected')
    expect(deferredRun?.deferred).toBe(true)
    expect(harness.sessionKey.value).toBe(SESSION_A)
    expect(requests(harness.socket)).toHaveLength(before)

    queue.resolve()
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(before + 2))
    const handoff = requests(harness.socket).slice(before)
    expect(handoff.map(frame => [frame.method, frame.params.key])).toEqual([
      ['sessions.messages.unsubscribe', SESSION_A],
      ['sessions.messages.subscribe', SESSION_B],
    ])
    reply(harness.socket, handoff[0]!, { unsubscribed: true })
    reply(harness.socket, handoff[1]!, subscribePayload(SESSION_B, 'B'))
    await switching

    expect(harness.sessionKey.value).toBe(SESSION_B)
    expect(keyedRequests(
      harness.socket,
      'sessions.messages.subscribe',
      SESSION_A,
    )).toHaveLength(1)
  })

  it('keeps A active when a pending queue failure follows a connected race', async () => {
    const queue = deferred<void>()
    const failure = new Error('pending queue failed')
    const harness = createHarness({
      switchPendingQueue: () => queue.promise,
    })
    rpc = harness.rpc
    const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
    await vi.waitFor(() => expect(requests(harness.socket)).toHaveLength(1))
    reply(harness.socket, requests(harness.socket)[0]!, subscribePayload(SESSION_A, 'A'))
    await initial.live
    const before = requests(harness.socket).length

    const switching = harness.runtime.switchToSession(SESSION_B)
    expect(harness.bootstrap.handleConnectionState('connected')?.deferred).toBe(true)
    queue.reject(failure)
    await expect(switching).rejects.toBe(failure)
    await flushMicrotasks()

    expect(harness.sessionKey.value).toBe(SESSION_A)
    expect(harness.persistSession).not.toHaveBeenCalled()
    expect(requests(harness.socket)).toHaveLength(before)
    expect(keyedRequests(
      harness.socket,
      'sessions.messages.unsubscribe',
      SESSION_A,
    )).toEqual([])
  })

  it('rebinds only the final target when the socket closes during handoff', async () => {
    vi.useFakeTimers()
    try {
      const queue = deferred<void>()
      const harness = createHarness({
        switchPendingQueue: () => queue.promise,
      })
      rpc = harness.rpc
      const offBridge = harness.attachConnectionBridge()
      const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
      await flushMicrotasks()
      const subscribeA = requests(harness.socket)[0]!
      reply(harness.socket, subscribeA, subscribePayload(SESSION_A, 'A'))
      await initial.live

      const switching = harness.runtime.switchToSession(SESSION_B)
      harness.socket.close(1006, 'test outage')
      expect(harness.rpc.state).toBe('disconnected')
      queue.resolve()
      await flushMicrotasks()
      expect(harness.sessionKey.value).toBe(SESSION_B)

      await vi.advanceTimersByTimeAsync(1_000)
      const replacement = SessionSwitchSocket.instances[1]!
      expect(replacement).toBeDefined()
      authenticate(replacement, 'conn-replacement')
      await flushMicrotasks()
      const replacementWire = requests(replacement)
      expect(replacementWire.map(frame => [frame.method, frame.params.key])).toEqual([
        ['sessions.messages.subscribe', SESSION_B],
      ])
      reply(replacement, replacementWire[0]!, subscribePayload(SESSION_B, 'B'))
      await switching

      expect(SessionSwitchSocket.instances).toHaveLength(2)
      expect(keyedRequests(
        replacement,
        'sessions.messages.subscribe',
        SESSION_A,
      )).toEqual([])
      expect(harness.appliedSnapshots.map(value => value.marker)).toEqual(['A', 'B'])
      offBridge()
      harness.rpc.disconnect()
      rpc = null
    } finally {
      vi.useRealTimers()
    }
  })

  it('converges after 50 fast switches without pending, listener, timer, or lease leaks', async () => {
    vi.useFakeTimers()
    try {
      const harness = createHarness()
      rpc = harness.rpc
      const generation = harness.rpc.connectionGeneration
      const gatewaySubscriptions = new Set<string>()
      const initial = harness.bootstrap.startSessionBootstrap({ includeHistory: false })
      await flushMicrotasks()
      const initialSubscribe = requests(harness.socket)[0]!
      gatewaySubscriptions.add(SESSION_A)
      reply(harness.socket, initialSubscribe, subscribePayload(SESSION_A, 'initial-A'))
      await initial.live
      await flushMicrotasks()

      const baselineListeners = listenerCount(harness.rpc)
      const baselineTimers = vi.getTimerCount()
      harness.stateTrace.length = 0

      for (let index = 0; index < 50; index += 1) {
        const target = index % 2 === 0 ? SESSION_B : SESSION_A
        const before = requests(harness.socket).length
        const switching = harness.runtime.switchToSession(target)
        await flushMicrotasks()
        const frames = requests(harness.socket).slice(before)
        expect(frames.map(frame => [frame.method, frame.params.key])).toEqual([
          ['sessions.messages.unsubscribe', index % 2 === 0 ? SESSION_A : SESSION_B],
          ['sessions.messages.subscribe', target],
        ])
        gatewaySubscriptions.delete(String(frames[0]!.params.key))
        reply(harness.socket, frames[0]!, { unsubscribed: true })
        gatewaySubscriptions.add(target)
        reply(harness.socket, frames[1]!, subscribePayload(target, `target-${index}`))
        await switching
        await flushMicrotasks()
      }

      expect(harness.sessionKey.value).toBe(SESSION_A)
      expect(gatewaySubscriptions).toEqual(new Set([SESSION_A]))
      expect(rpcInternals(harness.rpc)._pending.size).toBe(0)
      expect(rpcInternals(harness.rpc)._reconnectAttempt).toBe(0)
      expect(listenerCount(harness.rpc)).toBe(baselineListeners)
      expect(vi.getTimerCount()).toBe(baselineTimers)
      expect(SessionSwitchSocket.instances).toHaveLength(1)
      expect(harness.rpc.connectionGeneration).toBe(generation)
      expect(harness.stateTrace).not.toContain('disconnected')

      // With no production-only debug hook, one releasable lease is proven by
      // exactly one generation-pinned release; a second call emits nothing.
      const beforeRelease = requests(harness.socket).length
      harness.bootstrap.cancelSessionBootstrap()
      await flushMicrotasks()
      const releaseFrames = requests(harness.socket).slice(beforeRelease)
      expect(releaseFrames).toHaveLength(1)
      expect(releaseFrames[0]?.params.key).toBe(SESSION_A)
      reply(harness.socket, releaseFrames[0]!, { unsubscribed: true })
      await flushMicrotasks()
      harness.bootstrap.cancelSessionBootstrap()
      expect(requests(harness.socket)).toHaveLength(beforeRelease + 1)

      harness.rpc.disconnect()
      rpc = null
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the generation when navigation abandons held optional Session owners', async () => {
    rpc = new RpcClient()
    rpc.connect('ws://session-switch.test')
    const socket = SessionSwitchSocket.instances[0]!
    authenticate(socket, 'conn-optional-stable')
    const initialGeneration = rpc.connectionGeneration
    const owners = [
      {
        method: 'artifacts.list',
        params: { sessionKey: SESSION_A },
      },
      { method: 'workspaces.list', params: {} },
      { method: 'workspaces.list', params: {} },
    ]
    const controllers = owners.map(() => new AbortController())
    const abandoned = owners.map((owner, index) => rpc!.call(
      owner.method,
      owner.params,
      {
        timeoutMs: 7_000,
        signal: controllers[index]!.signal,
        timeoutAction: 'reconnect',
        abortAction: 'reject',
      },
    ).catch((error: unknown) => error))
    const staleFrames = requests(socket).slice()

    controllers.forEach(controller => controller.abort())
    await expect(Promise.all(abandoned)).resolves.toEqual([
      expect.any(RpcAbortError),
      expect.any(RpcAbortError),
      expect.any(RpcAbortError),
    ])
    expect(SessionSwitchSocket.instances).toHaveLength(1)
    expect(socket.readyState).toBe(SessionSwitchSocket.OPEN)
    expect(rpc.connectionGeneration).toBe(initialGeneration)

    const targetReads = owners.map(owner => rpc!.call(
      owner.method,
      owner.method === 'artifacts.list'
        ? { sessionKey: SESSION_B }
        : owner.params,
    ))
    const targetFrames = requests(socket).slice(staleFrames.length)
    targetFrames.forEach((frame, index) => reply(socket, frame, { owner: `b-${index}` }))
    await expect(Promise.all(targetReads)).resolves.toEqual([
      { owner: 'b-0' },
      { owner: 'b-1' },
      { owner: 'b-2' },
    ])

    staleFrames.forEach((frame, index) => reply(socket, frame, { owner: `a-${index}` }))
    expect(SessionSwitchSocket.instances).toHaveLength(1)
    expect(rpc.connectionGeneration).toBe(initialGeneration)
  })

  it('keeps an old-policy history cancellation local before and after send', async () => {
    rpc = new RpcClient()
    rpc.connect('ws://session-switch.test')
    const socket = SessionSwitchSocket.instances[0]!
    socket.receive({ type: 'event', event: 'connect.challenge' })
    const initialGeneration = rpc.connectionGeneration

    const waitController = new AbortController()
    const waiting = rpc.ready(
      7_000,
      waitController.signal,
      { timeoutAction: 'reject', abortAction: 'reject' },
    ).catch((error: unknown) => error)
    waitController.abort()
    await expect(waiting).resolves.toBeInstanceOf(RpcAbortError)
    expect(socket.readyState).toBe(SessionSwitchSocket.OPEN)
    expect(rpc.connectionGeneration).toBe(initialGeneration)

    socket.receive({
      protocol: 3,
      policy: {
        tick_interval_ms: 30_000,
        concurrent_history_reads: false,
      },
      server: { conn_id: 'conn-history-stable' },
    })
    const requestController = new AbortController()
    const history = rpc.call(
      'chat.history',
      { sessionKey: SESSION_A },
      {
        timeoutMs: 7_000,
        signal: requestController.signal,
        timeoutAction: 'reconnect',
        abortAction: 'reject',
      },
    ).catch((error: unknown) => error)
    expect(requests(socket).map(frame => frame.method)).toEqual(['chat.history'])

    requestController.abort()
    await expect(history).resolves.toBeInstanceOf(RpcAbortError)
    expect(socket.readyState).toBe(SessionSwitchSocket.OPEN)
    expect(SessionSwitchSocket.instances).toHaveLength(1)
    expect(rpc.connectionGeneration).toBe(initialGeneration)
  })
})
