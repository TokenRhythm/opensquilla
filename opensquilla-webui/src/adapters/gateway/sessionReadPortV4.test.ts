import { RpcClient, RpcTimeoutError } from '@/lib/rpc'
import { createConversationEventHub } from '@/modules/conversationEventHub'
import { createConversationEventTransport } from './conversationEventTransport'
import { TransportFlowV4 } from './transportFlowV4'
import type { TransportEventHandler, TransportLaneRetireReceipt } from './transportTypes'
import { createSessionReadLifecycle, type SessionReadPortLease } from '@/modules/sessionReadLifecycle'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import { createConversationSubscriptionLifecycle } from '@/modules/conversationSubscriptionLifecycle'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import type { RpcCallOptions } from '@/lib/rpc'
import { CHAT_HISTORY_METHOD, type ChatHistoryResult } from '@/contracts/generated/v4/chatHistory'
import {
  SESSIONS_MESSAGES_HYDRATE_METHOD,
  type SessionsMessagesHydrateResult,
} from '@/contracts/generated/v4/sessionsMessagesHydrate'
import {
  SESSIONS_MESSAGES_SNAPSHOT_METHOD,
  type SessionsMessagesSnapshotResult,
} from '@/contracts/generated/v4/sessionsMessagesSnapshot'
import {
  SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
  type SessionsMessagesSubscribeResult,
} from '@/contracts/generated/v4/sessionsMessagesSubscribe'
import { SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD } from '@/contracts/generated/v4/sessionsMessagesUnsubscribe'
import {
  SessionReadContractError,
  SessionReadFailure,
  SessionReadHistoryCursorError,
  SessionReadSessionMissingError,
  type SessionReadMetadata,
} from '@/modules/sessionReadLifecycle'
import type { ConversationEvent } from '@/modules/conversationEvents'
import type { InterruptViewState } from '@/types/parts'
import { useChatApprovals } from '@/composables/chat/useChatApprovals'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { createV4SessionReadPort } from './sessionReadPortV4'
import { mapSessionReadError } from './sessionReadErrorMapping'

type Call = {
  method: string
  params?: Record<string, unknown>
  options?: RpcCallOptions
}

interface Deferred<T> {
  readonly promise: Promise<T>
  resolve(value: T): void
  reject(error: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function metadataFields(hydrationComplete = true) {
  return {
    workspaceId: 'workspace-1',
    projectWorkspace: {
      id: 'workspace-1',
      display_name: 'Workspace One',
      nested_context: { snake_value: true },
    },
    projectWorkspaceDeferred: false,
    active_task_group_ids: [],
    run_mode_lock: { locked: true, runMode: 'safe' as const, source: 'profile' },
    pendingUserInputs: [{ request_id: 'input-1' }],
    collaboration: { mode_name: 'delegate' },
    routing: { mode: 'recommended' },
    planPresentations: [{ revisionId: 'plan-1', dismissed: true, stateRevision: 2 }],
    currentPlan: { plan_id: 'plan-1' },
    activePlanRun: { run_id: 'run-1' },
    goal: { goal_id: 'goal-1' },
    goalSnapshotStreamSeq: 6,
    tasks: [{ task_id: 'task-1' }],
    active_task: { task_id: 'task-1' },
    last_task: { task_id: 'task-0' },
    run_status: 'running',
    queued_task_ids: ['task-2'],
    epoch: 3,
    hydration_complete: hydrationComplete,
    deferred_fields: hydrationComplete ? [] : ['routing'],
    future_metadata: { snake_value: true },
  }
}

function subscribeResult(
  patch: Partial<SessionsMessagesSubscribeResult> = {},
): SessionsMessagesSubscribeResult {
  return {
    ...metadataFields(),
    subscribed: true,
    key: 'alpha',
    stream_generation: 'stream-1',
    current_stream_seq: 9,
    replay_complete: true,
    replay_gap_reason: null,
    replayed_count: 0,
    ...patch,
  }
}

function hydrateResult(
  patch: Partial<SessionsMessagesHydrateResult> = {},
): SessionsMessagesHydrateResult {
  return {
    ...metadataFields(),
    key: 'alpha',
    hydration_complete: true,
    ...patch,
  }
}

function snapshotResult(
  patch: Partial<SessionsMessagesSnapshotResult> = {},
): SessionsMessagesSnapshotResult {
  return {
    key: 'alpha',
    task_id: 'task-snapshot',
    stream_generation: 'stream-1',
    current_stream_seq: 8,
    events: [{
      event: 'session.event.text_delta',
      payload: {
        task_id: 'task-snapshot',
        text_delta: 'hello',
        input: { snake_value: true },
      },
    }],
    ...patch,
  }
}

function historyResult(
  patch: Partial<ChatHistoryResult> = {},
): ChatHistoryResult {
  return {
    messages: [{
      id: 41,
      message_id: 'message-1',
      transcript_id: 42,
      role: 'assistant',
      text: 'hello',
      timestamp: 1_725_199_200,
      reasoning_content: '  thinking exactly  ',
      router_decision: { tier: 'c1' },
      artifacts: [{ artifact_id: 'artifact-1' }],
      tool_calls: [{ tool_name: 'read' }],
      timeline: [{ segment_kind: 'thinking' }],
      attachments: [{ attachment_id: 'attachment-1' }],
      prompt_annotations: [{ annotation_kind: 'cache' }],
      turn_context: {
        turn_id: 'turn-1',
        promoted_turn_id: 'turn-promoted',
        applied_iteration: 2,
        activity_markers: [{ marker_id: 'marker-1' }],
        run_mode: 'safe',
      },
      turn_usage: { total_tokens: 8 },
      input: 3,
      output: 5,
      model: 'model-1',
      provenance_kind: 'forwarded',
      provenance_source_session_key: 'source-session',
      provenance_source_tool: 'delegate',
      additive_message_field: { nested_value: true },
    }],
    has_more: true,
    oldest_cursor: 'cursor-1',
    newest_cursor: 'cursor-9',
    history_scope: 'latest_window',
    loaded_count: 1,
    page_size: 100,
    canonical_available: false,
    canonical_complete: true,
    compaction_summaries: [{
      id: 7,
      compaction_id: 'compact-1',
      compaction_index: 2,
      trigger_reason: 'budget',
      summary_text: 'summary',
      summary_format: 'markdown',
      coverage_status: 'complete',
      removed_count: 8,
      kept_count: 3,
      covered_through_id: 40,
      created_at: 1_725_199_100,
      future_summary_field: 'kept',
    }],
    turn_outcomes: [{
      turn_id: 'turn-1',
      task_id: 'task-1',
      status: 'succeeded',
      started_at: 1,
      finished_at: 2,
      outcome: { finish_reason: 'stop' },
      error_class: 'usage_accounting_busy',
      retryable: true,
      activity_snapshot: { task_id: 'task-1', phase_name: 'finalize' },
      usage: { input_tokens: 3, output_tokens: 5 },
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      retry_after_ms: 100,
      user_message_id: 'message-user-1',
      terminal_message: 'retry safely',
      future_outcome_field: true,
    }],
    future_history_field: { nested_value: true },
    ...patch,
  }
}

function makeHarness() {
  let generation = 7
  const calls: Call[] = []
  const results = new Map<string, unknown>([
    [SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribeResult()],
    [SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult()],
    [SESSIONS_MESSAGES_HYDRATE_METHOD, hydrateResult()],
    [CHAT_HISTORY_METHOD, historyResult()],
    [SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD, null],
  ])
  const requestMock = vi.fn((
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<unknown> => {
    calls.push({ method, params, options })
    options?.onSent?.(generation)
    const result = results.get(method)
    if (result instanceof Error) return Promise.reject(result)
    return Promise.resolve(result)
  })
  const rpc = {
    request<T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<T> {
      return requestMock(method, params, options) as Promise<T>
    },
    ready: vi.fn(async () => undefined),
    get generation() { return generation },
  }
  return {
    rpc,
    calls,
    results,
    requestMock,
    setGeneration(value: number) { generation = value },
  }
}

const openRequest = (
  signal = new AbortController().signal,
  includeInitialHistory = true,
) => ({
  sessionKey: 'alpha',
  includeInitialHistory,
  resumeFrom: { streamGeneration: 'stream-0', streamSeq: 4 },
  signal,
})

async function flushAsyncWork() {
  for (let index = 0; index < 12; index++) await Promise.resolve()
}

const SNAPSHOT_READ = 'sessions.messages.snapshot.read'
const SNAPSHOT_RESUME = 'sessions.messages.resume'
const SNAPSHOT_RELEASE = 'sessions.messages.snapshot.release'

function installationHarness(modern = true) {
  const base = makeHarness()
  const snapshot = snapshotResult()
  const bytes = Buffer.from(JSON.stringify(snapshot))
  const resume = vi.fn(async (params: Record<string, unknown>) => ({
    ...params, session_id: null, session_epoch: 3, replay_to_seq: snapshot.current_stream_seq,
  }))
  const consume = vi.fn(async () => {})
  const resumeFlow = vi.fn(async () => {})
  base.requestMock.mockImplementation(async (method, params = {}, options) => {
    base.calls.push({ method, params, options })
    options?.onSent?.(base.rpc.generation)
    if (method === SNAPSHOT_READ) return {
      key: 'alpha', sync_revision: params.sync_revision, snapshot_id: `snapshot-${params.sync_revision}`,
      segment_index: 0, segment_count: 1, byte_length: bytes.length,
      encoding: 'base64-json-utf8', data: bytes.toString('base64'),
      stream_generation: snapshot.stream_generation, current_stream_seq: snapshot.current_stream_seq,
      task_id: snapshot.task_id, session_id: null, session_epoch: 3,
    }
    if (method === SNAPSHOT_RESUME) return resume(params)
    if (method === SNAPSHOT_RELEASE) return { ...params, retired: true }
    const result = base.results.get(method)
    if (result instanceof Error) throw result
    return result
  })
  const rpc = {
    ...base.rpc,
    get generation() { return base.rpc.generation },
    supports: (method: string) => method === SNAPSHOT_READ || modern,
    waitForConsumption: consume,
    resumeFlow,
  }
  return { ...base, rpc, resume, consume, resumeFlow }
}

const READ_OPEN_V2 = 'sessions.read.open.v2'
const READ_STATE_V2 = 'sessions.read.state.v2'
const READ_INSTALL_V2 = 'sessions.read.install.v2'
const READ_CLOSE_V2 = 'sessions.read.close.v2'
const HISTORY_V2 = 'sessions.history.page.v2'

function v2AdmissionHarness() {
  const base = makeHarness()
  const progress = { status: 'installed', base_seq: 9, target_seq: 9, next_seq: 10, consumed_through_seq: 9 }
  base.results.set(READ_OPEN_V2, {
    lease_id: 'lease-alpha', recovery_id: 'recovery-alpha', session_id: 'id-alpha',
    session_epoch: 3, connection_epoch: 'connection', subscription_epoch: 'subscription-alpha',
    state_revision: 1, base_stream_generation: 'stream-1', base_stream_seq: 9,
    state_manifest: { length: 0, sha256: 'a'.repeat(64), schema_version: 1, chunks: [] },
  })
  base.results.set(READ_STATE_V2, {
    lease_id: 'lease-alpha', recovery_id: 'recovery-alpha', session_id: 'id-alpha',
    session_epoch: 3, state_revision: 1, status: 'installed', progress,
  })
  base.results.set(READ_INSTALL_V2, {
    lease_id: 'lease-alpha', recovery_id: 'recovery-alpha', status: 'installed', progress,
  })
  base.results.set(READ_CLOSE_V2, { lease_id: 'lease-alpha', closed: true, status: 'retired' })
  base.results.set(HISTORY_V2, {
    session_id: 'id-alpha', session_epoch: 3, projection_revision: 1,
    before_cursor: null, after_cursor: null, has_more_before: false, has_more_after: false,
    complete_for_requested_window: true, canonical_available: true, canonical_complete: true,
    history_scope: 'complete', compaction_summaries: [], turn_outcomes: [], items: [],
  })
  const methods = new Set([READ_OPEN_V2, READ_STATE_V2, READ_INSTALL_V2, READ_CLOSE_V2, HISTORY_V2])
  const original = base.requestMock.getMockImplementation()!
  base.requestMock.mockImplementation(async (method, params, options) => {
    const result = await original(method, params, options)
    if (!result || typeof result !== 'object' || params?.key === 'alpha') return result
    const key = String(params?.key)
    const value = result as Record<string, unknown>
    return {
      ...value,
      ...('key' in value ? { key } : {}),
      ...('lease_id' in value ? { lease_id: `lease-${key}` } : {}),
      ...('recovery_id' in value ? { recovery_id: `recovery-${key}` } : {}),
      ...('session_id' in value ? { session_id: `id-${key}` } : {}),
    }
  })
  return {
    ...base,
    rpc: {
      ...base.rpc,
      get generation() { return base.rpc.generation },
      sessionReadV2: true,
      supports: (method: string) => methods.has(method),
      waitForConsumption: vi.fn(async () => {}),
    },
  }
}

function snapshotV2ConsumptionHarness(target = 10) {
  const h = v2AdmissionHarness()
  const listeners = new Map<string, TransportEventHandler>()
  const emit = (event: string, ...args: unknown[]) => listeners.get(event)?.(...args)
  const flow = new TransportFlowV4({
    connectionGeneration: h.rpc.generation,
    on(event, handler) { listeners.set(event, handler); return () => { listeners.delete(event) } },
    enableConsumptionFlow() {},
    consumeEvent: async () => 'applied',
    recoverGap: async () => true,
    request: async <T>(_method: string, params: Record<string, unknown> = {}) => ({
      connection_epoch: params.connection_epoch, consumed: params.consumed ?? [],
      staged_recovery: params.staged_recovery ?? [], discarded_lanes: params.discarded_lanes ?? [],
      lane_count: 1,
    }) as T,
  })
  emit('_hello', { policy: { transport_flow: {
    delivery_epoch: 'connection', window_frames: 128, window_bytes: 4 * 1024 * 1024,
    capability: 'transport.session-flow.v2',
  } } })
  const snapshot = snapshotResult({ task_id: null, current_stream_seq: 10, events: [] })
  const bytes = Buffer.from(JSON.stringify(snapshot))
  let consumed = 9
  let snapshotSessionId = 'id-alpha'
  let resumeError: Error | null = null
  let installTarget: number | null = null
  const progress = () => ({
    status: consumed >= target ? 'installed' : 'catching_up', base_seq: 9,
    target_seq: target, next_seq: consumed + 1, consumed_through_seq: consumed,
  })
  const originalRequest = h.requestMock.getMockImplementation()!
  h.requestMock.mockImplementation(async (method, params = {}, options) => {
    if (![SNAPSHOT_READ, SNAPSHOT_RESUME, SNAPSHOT_RELEASE, READ_STATE_V2, READ_INSTALL_V2].includes(method)) {
      return originalRequest(method, params, options)
    }
    h.calls.push({ method, params, options })
    options?.onSent?.(h.rpc.generation)
    if (method === SNAPSHOT_READ) return {
      key: 'alpha', sync_revision: params.sync_revision, snapshot_id: `snapshot-${params.sync_revision}`,
      segment_index: 0, segment_count: 1, byte_length: bytes.length,
      encoding: 'base64-json-utf8', data: bytes.toString('base64'),
      stream_generation: snapshot.stream_generation, current_stream_seq: snapshot.current_stream_seq,
      task_id: null, session_id: snapshotSessionId, session_epoch: 3,
    }
    if (method === SNAPSHOT_RESUME) {
      if (resumeError) throw resumeError
      return {
        ...params, session_id: snapshotSessionId, session_epoch: 3, replay_to_seq: snapshot.current_stream_seq,
      }
    }
    if (method === SNAPSHOT_RELEASE) return { ...params, retired: true }
    if (method === READ_INSTALL_V2 && typeof params.consumed_through_seq === 'number') {
      consumed = params.consumed_through_seq
      if (installTarget !== null) target = installTarget
    }
    return {
      lease_id: 'lease-alpha', recovery_id: 'recovery-alpha', status: progress().status, progress: progress(),
      ...(method === READ_STATE_V2 ? { session_id: 'id-alpha', session_epoch: 3, state_revision: 9 } : {}),
    }
  })
  const waitForConsumption = vi.fn(flow.waitForConsumption.bind(flow))
  const rpc = {
    ...h.rpc,
    supports: (method: string) => h.rpc.supports(method)
      || [SNAPSHOT_READ, SNAPSHOT_RESUME, SNAPSHOT_RELEASE].includes(method),
    waitForConsumption,
    recoveryVersion: (key: string) => flow.recoveryVersion(key),
    snapshotInstalled: (key: string, version: string) => flow.snapshotInstalled(key, version),
  }
  return {
    ...h, rpc, flow, waitForConsumption,
    replaceSnapshotSessionId(value: string) { snapshotSessionId = value },
    failResume(error: Error) { resumeError = error },
    advanceTargetAtInstall(value: number) { installTarget = value },
    deliver(sequence: number) {
      emit('*', 'session.event.text_delta', {
        session_key: 'alpha', stream_generation: 'stream-1', stream_seq: sequence, text: 'tail',
      }, {
        flow: { delivery_epoch: 'connection', delivery_id: 1 },
        session_flow_v2: { connection_epoch: 'connection', subscription_epoch: 'subscription-alpha', delivery_id: 1 },
      })
    },
  }
}

describe('read-v2 consumption covered by an installed snapshot', () => {
  it.each(['initial', 'reconcile'])('uses the newer %s snapshot without inventing transport receipts', async source => {
    const h = snapshotV2ConsumptionHarness()
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      let live = await lease.live
      if (source === 'reconcile') live = await lease.reconcile()
      expect(live.snapshot?.currentStreamSeq).toBe(10)
      // The caller applies the returned snapshot before confirming its install.
      await live.confirmInstalled!()
      expect(h.waitForConsumption.mock.calls).toEqual([
        ['alpha', { streamGeneration: 'stream-1', fromSeq: 10, toSeq: 10 }],
      ])
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2).slice(-1)[0]?.params)
        .toMatchObject({ consumed_through_seq: 10, ack_through_seq: 10 })
      expect(h.flow.diagnostics.ackDeliveryId).toBe(0)
      await expect(h.flow.waitForConsumption('alpha', {
        streamGeneration: 'stream-1', fromSeq: 9, toSeq: 10,
      })).rejects.toMatchObject({ code: 'SNAPSHOT_STALE' })
    } finally { await lease.close(); h.flow.close() }
  })

  it('does not acknowledge a missing event after the snapshot watermark', async () => {
    const h = snapshotV2ConsumptionHarness(11)
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      const live = await lease.live
      await expect(live.confirmInstalled!()).rejects.toMatchObject({
        message: 'Snapshot replay tail was not consumed.', retryable: true,
      })
      expect(h.waitForConsumption).toHaveBeenLastCalledWith('alpha', {
        streamGeneration: 'stream-1', fromSeq: 10, toSeq: 11,
      })
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2)).toHaveLength(1)
      expect(h.flow.diagnostics.ackDeliveryId).toBe(0)
    } finally { await lease.close(); h.flow.close() }
  })

  it('installs when the real event consumer owns the tail after the snapshot', async () => {
    const h = snapshotV2ConsumptionHarness(11)
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      const live = await lease.live
      h.deliver(11)
      await flushAsyncWork()
      await live.confirmInstalled!()
      expect(h.waitForConsumption).toHaveBeenLastCalledWith('alpha', {
        streamGeneration: 'stream-1', fromSeq: 10, toSeq: 11,
      })
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2).slice(-1)[0]?.params)
        .toMatchObject({ consumed_through_seq: 11, ack_through_seq: 11 })
      expect(h.flow.diagnostics.ackDeliveryId).toBe(1)
    } finally { await lease.close(); h.flow.close() }
  })

  it('refuses consumption proof from a different durable session identity', async () => {
    const h = snapshotV2ConsumptionHarness()
    h.replaceSnapshotSessionId('different-session')
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      const live = await lease.live
      await expect(live.confirmInstalled!()).rejects.toThrow('Installed snapshot does not match')
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2)).toHaveLength(1)
    } finally { await lease.close(); h.flow.close() }
  })

  it('does not use the snapshot watermark before the server confirms its installation', async () => {
    const h = snapshotV2ConsumptionHarness()
    h.failResume(Object.assign(new Error('Snapshot no longer current'), { code: 'SNAPSHOT_STALE' }))
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      const live = await lease.live
      await expect(live.confirmInstalled!()).rejects.toThrow('Snapshot no longer current')
      expect(h.waitForConsumption).not.toHaveBeenCalled()
      expect(h.calls.filter(call => call.method === READ_STATE_V2)).toHaveLength(0)
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2)).toHaveLength(1)
    } finally { await lease.close(); h.flow.close() }
  })

  it('rejects installation when the server still reports catching_up', async () => {
    const h = snapshotV2ConsumptionHarness()
    h.advanceTargetAtInstall(11)
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(undefined, false))
    try {
      const live = await lease.live
      await expect(live.confirmInstalled!()).rejects.toMatchObject({ kind: 'busy', retryable: true })
      expect(h.calls.filter(call => call.method === READ_INSTALL_V2).slice(-1)[0]?.params)
        .toMatchObject({ consumed_through_seq: 10, ack_through_seq: 10 })
      expect(h.flow.diagnostics.ackDeliveryId).toBe(0)
    } finally { await lease.close(); h.flow.close() }
  })
})

describe('read-v2 admission and retirement ownership', () => {
  const historyRequest = (signal: AbortSignal) => ({ direction: 'latest' as const, limit: 100, signal })

  it.each(['REBASE_REQUIRED', 'READ_STALE'])('projects %s as a read-base replacement request', code => {
    expect(mapSessionReadError(Object.assign(new Error('Reload state'), { code })))
      .toMatchObject({ kind: 'rebase-required', retryable: true })
  })

  it.each(['response', 'send'])('isolates a v2 history %s failure at the actual send boundary', async failure => {
    const h = v2AdmissionHarness()
    h.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribeResult({ ...metadataFields(false) }))
    const error = Object.assign(new Error('History failed'), { code: 'CONTENT_TOO_LARGE' })
    const original = h.requestMock.getMockImplementation()!
    h.requestMock.mockImplementation(async (method, params, options) => {
      if (method === HISTORY_V2) {
        if (failure === 'response') options?.onSent?.(h.rpc.generation)
        throw error
      }
      return original(method, params, options)
    })
    const request = openRequest()
    const lease = createV4SessionReadPort(h.rpc).open(request)
    try {
      const results = await Promise.allSettled([
        lease.live, lease.metadata, lease.criticalRequestsQueued,
        lease.readHistory(historyRequest(request.signal)),
      ])
      expect(results.map(result => result.status)).toEqual(failure === 'response'
        ? ['fulfilled', 'fulfilled', 'fulfilled', 'rejected']
        : ['rejected', 'rejected', 'rejected', 'rejected'])
      expect(h.calls.some(call => call.method === SESSIONS_MESSAGES_HYDRATE_METHOD)).toBe(failure === 'response')
    } finally { await lease.close() }
  })

  it.each(['closed', 'replaced', 'generation'])('does not promote a late rebase error from a %s owner', async change => {
    const h = v2AdmissionHarness()
    const port = createV4SessionReadPort(h.rpc)
    const lease = port.open(openRequest(undefined, false))
    await lease.live
    const delayed = deferred<unknown>()
    const install = h.results.get(READ_INSTALL_V2)
    h.results.set(READ_INSTALL_V2, delayed.promise)
    const recovery = expect(lease.reconcile()).rejects.toMatchObject({ kind: 'aborted' })
    await vi.waitFor(() => expect(h.calls.filter(call => call.method === READ_INSTALL_V2)).toHaveLength(2))
    let replacement: SessionReadPortLease | undefined
    if (change === 'closed') await lease.close()
    else if (change === 'generation') h.setGeneration(h.rpc.generation + 1)
    else {
      h.results.set(READ_INSTALL_V2, install)
      replacement = port.open(openRequest(undefined, false))
      await replacement.live
    }
    delayed.reject(Object.assign(new Error('Old base expired'), { code: 'REBASE_REQUIRED' }))
    await recovery
    const closeCount = h.calls.filter(call => call.method === READ_CLOSE_V2).length
    await lease.close()
    expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(closeCount)
    await replacement?.close()
  })

  it('keeps an unmaterialized draft live across reconnect, then opens v2 after acceptance', async () => {
    const h = v2AdmissionHarness()
    const original = h.requestMock.getMockImplementation()!
    let materialized = false
    h.requestMock.mockImplementation(async (method, params, options) => {
      if (method === READ_OPEN_V2 && !materialized) {
        throw Object.assign(new Error('Session not found'), { code: 'NOT_FOUND' })
      }
      return original(method, params, options)
    })
    const port = createV4SessionReadPort(h.rpc)
    for (let generation = 1; generation <= 2; generation++) {
      h.setGeneration(generation)
      const draft = port.open({ ...openRequest(), includeInitialHistory: false, provisionalDraft: true })
      try {
        const [live] = await Promise.all([draft.live, draft.metadata, draft.criticalRequestsQueued])
        await live.confirmInstalled?.()
        expect(live.sessionKey).toBe('alpha')
      } finally { await draft.close() }
    }
    expect(h.requestMock.mock.calls.filter(([method]) => method === READ_OPEN_V2)).toHaveLength(0)
    expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(2)
    materialized = true
    for (let generation = 2; generation <= 3; generation++) {
      h.setGeneration(generation)
      const durable = port.open(openRequest())
      try {
        const [live, page] = await Promise.all([durable.live, durable.readHistory(historyRequest(new AbortController().signal))])
        await live.confirmInstalled?.()
        expect(page.messages).toEqual([])
      } finally { await durable.close() }
    }
    expect(h.calls.filter(call => call.method === READ_OPEN_V2)).toHaveLength(2)
    expect(h.calls.filter(call => call.method === HISTORY_V2)).toHaveLength(2)
    expect(h.calls.some(call => call.method === CHAT_HISTORY_METHOD)).toBe(false)
  })

  it('retains NOT_FOUND for an existing session even when initial history is skipped', async () => {
    const h = v2AdmissionHarness()
    const original = h.requestMock.getMockImplementation()!
    h.requestMock.mockImplementation(async (method, params, options) => {
      if (method === READ_OPEN_V2) throw Object.assign(new Error('Session not found'), { code: 'NOT_FOUND' })
      return original(method, params, options)
    })
    const lease = createV4SessionReadPort(h.rpc).open({ ...openRequest(), includeInitialHistory: false })
    try {
      await expect(lease.live).rejects.toBeInstanceOf(SessionReadSessionMissingError)
      await expect(lease.metadata).rejects.toBeInstanceOf(SessionReadSessionMissingError)
      expect(h.requestMock.mock.calls.some(([method]) => method === READ_OPEN_V2)).toBe(true)
    } finally { await lease.close() }
  })

  it('selects read-v2 after readiness negotiates capabilities for an already opened lease', async () => {
    const h = v2AdmissionHarness()
    const ready = deferred<void>()
    let negotiated = false
    const rpc = {
      ...h.rpc,
      get sessionReadV2() { return negotiated },
      supports: (method: string) => negotiated && h.rpc.supports(method),
      ready: vi.fn(() => ready.promise),
    }
    const signal = new AbortController().signal
    const lease = createV4SessionReadPort(rpc).open(openRequest(signal))
    const history = lease.readHistory(historyRequest(signal))
    try {
      expect(h.calls).toEqual([])
      negotiated = true
      ready.resolve()
      const [live, page] = await Promise.all([lease.live, history, lease.metadata, lease.criticalRequestsQueued])
      await live.confirmInstalled?.()
      expect(h.calls.some(call => call.method === READ_OPEN_V2)).toBe(true)
      expect(h.calls.some(call => call.method === HISTORY_V2)).toBe(true)
      expect(h.calls.some(call => call.method === CHAT_HISTORY_METHOD)).toBe(false)
      expect(page.messages).toEqual([])
    } finally { await lease.close() }
  })

  it.each([SESSIONS_MESSAGES_SUBSCRIBE_METHOD, READ_OPEN_V2, READ_INSTALL_V2])(
    'recovers a failed first %s through shared live/history/metadata admission', async method => {
      const h = v2AdmissionHarness()
      const successful = h.results.get(method)
      h.results.set(method, new RpcTimeoutError(method, 7_000))
      const signal = new AbortController().signal
      const lease = createV4SessionReadPort(h.rpc).open(openRequest(signal))
      try {
        await Promise.all([
          expect(lease.live).rejects.toMatchObject({ kind: 'timeout' }),
          expect(lease.metadata).rejects.toMatchObject({ kind: 'timeout' }),
          expect(lease.criticalRequestsQueued).rejects.toMatchObject({ kind: 'timeout' }),
          expect(lease.readHistory(historyRequest(signal))).rejects.toMatchObject({ kind: 'timeout' }),
        ])
        h.results.set(method, successful)
        const [live, page, metadata] = await Promise.all([
          lease.reconcile(), lease.readHistory(historyRequest(signal)), lease.retryMetadata(),
        ])
        await live.confirmInstalled!()
        expect(page.messages).toEqual([])
        expect(metadata.hydrationComplete).toBe(true)
        expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD))
          .toHaveLength(method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD ? 2 : 1)
        expect(h.calls.filter(call => call.method === READ_OPEN_V2))
          .toHaveLength(method === READ_OPEN_V2 ? 2 : 1)
        expect(h.calls.filter(call => call.method === READ_INSTALL_V2 && call.params?.base_applied))
          .toHaveLength(method === READ_INSTALL_V2 ? 2 : 1)
        expect(h.calls.filter(call => call.method === HISTORY_V2)).toHaveLength(1)
        expect(h.calls.some(call => call.method === READ_STATE_V2)).toBe(true)
      } finally { await lease.close() }
    },
  )

  it('queues old A unsubscribe before a delayed read-close response can reach replacement A', async () => {
    vi.useFakeTimers()
    const h = v2AdmissionHarness()
    const port = createV4SessionReadPort(h.rpc)
    const old = port.open(openRequest())
    await old.live
    const delayed = deferred<unknown>()
    const original = h.requestMock.getMockImplementation()!
    let held = false
    h.requestMock.mockImplementation((method, params, options) => {
      if (!held && method === READ_CLOSE_V2 && params?.key === 'alpha') {
        held = true
        h.calls.push({ method, params, options })
        return delayed.promise
      }
      return original(method, params, options)
    })
    const oldClose = old.close()
    const beta = port.open({ ...openRequest(), sessionKey: 'beta' })
    await beta.live
    await beta.close()
    const replacement = port.open(openRequest())
    try {
      await replacement.live
      const unsubscribe = h.calls.findIndex(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)
      const replacementSubscribe = h.calls.map(call => call.method).lastIndexOf(SESSIONS_MESSAGES_SUBSCRIBE_METHOD)
      expect(unsubscribe).toBeGreaterThan(-1)
      expect(unsubscribe).toBeLessThan(replacementSubscribe)
      const countBeforeReply = h.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD).length
      setTimeout(() => delayed.resolve(h.results.get(READ_CLOSE_V2)), 200)
      await vi.advanceTimersByTimeAsync(200)
      await oldClose
      expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(countBeforeReply)
      await expect(replacement.reconcile()).resolves.toMatchObject({ sessionKey: 'alpha' })
    } finally {
      await replacement.close()
      vi.useRealTimers()
    }
  })

  it.each(['read-close-throw', 'read-close-reject', 'unsubscribe-throw'] as const)(
    'starts both cleanup requests despite %s', async failure => {
      const h = v2AdmissionHarness()
      const lease = createV4SessionReadPort(h.rpc).open(openRequest())
      await lease.live
      const original = h.requestMock.getMockImplementation()!
      const failedMethod = failure === 'unsubscribe-throw' ? SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD : READ_CLOSE_V2
      h.requestMock.mockImplementation((method, params, options) => {
        if (method === failedMethod) {
          h.calls.push({ method, params, options })
          if (failure === 'read-close-reject') return Promise.reject(new Error(failure))
          throw new Error(failure)
        }
        return original(method, params, options)
      })
      await expect(lease.close()).rejects.toThrow(failure)
      expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(1)
      expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(1)
      await lease.close()
      expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(1)
    },
  )

  it('owns an opened lease while base installation is pending and never publishes it after close', async () => {
    const h = v2AdmissionHarness()
    const delayed = deferred<unknown>()
    const installed = h.results.get(READ_INSTALL_V2)
    h.results.set(READ_INSTALL_V2, delayed.promise)
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(new AbortController().signal, false))
    const live = expect(lease.live).rejects.toMatchObject({ kind: 'aborted' })
    const metadata = expect(lease.metadata).rejects.toMatchObject({ kind: 'aborted' })
    for (let index = 0; index < 12; index++) await flushAsyncWork()
    expect(h.calls.some(call => call.method === READ_INSTALL_V2)).toBe(true)
    await lease.close()
    expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(1)
    delayed.resolve(installed)
    await Promise.all([live, metadata])
    await expect(lease.reconcile()).rejects.toMatchObject({ name: 'AbortError' })
    expect(h.calls.some(call => call.method === READ_STATE_V2)).toBe(false)
  })

  it.each(['closed', 'aborted', 'replacement'] as const)('fences a late open result after %s', async reason => {
    const h = v2AdmissionHarness()
    const opened = h.results.get(READ_OPEN_V2)
    const delayed = deferred<unknown>()
    h.results.set(READ_OPEN_V2, delayed.promise)
    const port = createV4SessionReadPort(h.rpc)
    const controller = new AbortController()
    const old = port.open(openRequest(controller.signal, false))
    const rejected = Promise.all([
      expect(old.live).rejects.toMatchObject({ kind: 'aborted' }),
      expect(old.metadata).rejects.toMatchObject({ kind: 'aborted' }),
    ])
    for (let index = 0; index < 12; index++) await flushAsyncWork()
    expect(h.calls.filter(call => call.method === READ_OPEN_V2)).toHaveLength(1)
    if (reason === 'aborted') controller.abort()
    else await old.close()
    h.results.set(READ_OPEN_V2, opened)
    const replacement = reason === 'replacement' ? port.open(openRequest()) : null
    if (replacement) await replacement.live
    delayed.resolve(opened)
    await rejected
    expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(replacement ? 0 : 1)
    if (replacement) {
      await expect(replacement.reconcile()).resolves.toMatchObject({ sessionKey: 'alpha' })
      await replacement.close()
      expect(h.calls.filter(call => call.method === READ_CLOSE_V2)).toHaveLength(1)
    }
    await old.close()
  })
})

describe('SessionReadPort installation error boundary', () => {
  it('stops automatic flow recovery at a terminal snapshot without ACKing its unowned delivery', async () => {
    vi.useFakeTimers()
    const h = installationHarness()
    let flow: TransportFlowV4 | undefined
    const rpc = {
      ...h.rpc,
      recoveryVersion: (key: string) => flow?.recoveryVersion(key) ?? 'before-flow',
      snapshotInstalled: (key: string, version: string) => flow?.snapshotInstalled(key, version),
    }
    const originalRequest = h.requestMock.getMockImplementation()!
    h.requestMock.mockImplementation(async (method, params, options) => {
      if (method === SNAPSHOT_READ) {
        h.calls.push({ method, params, options })
        options?.onSent?.(h.rpc.generation)
        throw Object.assign(new Error('snapshot too large'), { code: 'SNAPSHOT_TOO_LARGE' })
      }
      return originalRequest(method, params, options)
    })
    let lease = createV4SessionReadPort(rpc).open(openRequest(undefined, false))
    await expect(lease.live).rejects.toMatchObject({ kind: 'too-large', retryable: false })
    const client = new RpcClient()
    const listeners = new Map<string, Set<TransportEventHandler>>()
    const on = (event: string, handler: TransportEventHandler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => { listeners.get(event)?.delete(handler) }
    }
    const emit = (event: string, ...args: unknown[]) => {
      for (const handler of listeners.get(event) ?? []) handler(...args)
    }
    const hub = createConversationEventHub(createConversationEventTransport({
      subscribe: (event, handler) => ({ close: on(event, handler) }),
      subscribeGap: handler => ({ close: client.onGap(handler) }),
    }))
    hub.prepareReadRetirement('alpha')
    hub.prepareReadRetirement('beta')
    const reconcile = vi.fn(async () => { await lease.reconcile(); return true })
    const beta = vi.fn(async () => true)
    hub.observeRecoveryRequired(scope => scope.keys[0] === 'alpha' ? reconcile() : beta())
    const control = vi.fn(async (_method: string, params: Record<string, unknown> = {}) => ({
      delivery_epoch: params.delivery_epoch, ack_delivery_id: params.ack_delivery_id,
      dirty_keys: [], global_dirty: false,
    }))
    flow = new TransportFlowV4({
      connectionGeneration: 1, on, enableConsumptionFlow() {},
      consumeEvent: async () => { throw new Error('Consumer requires authoritative recovery') },
      recoverGap: detail => client.recoverGap(detail), supportsRecovery: () => true,
      request: async <T>(method: string, params?: Record<string, unknown>) => control(method, params) as Promise<T>,
    })
    try {
      emit('_hello', { policy: { transport_flow: { delivery_epoch: 'epoch', window_frames: 128, window_bytes: 4194304 } } })
      for (const [id, key] of [[1, 'beta'], [2, 'alpha']] as const) emit('*', 'session.event.text_delta', {
        session_key: key, text_delta: 'x',
      }, { flow: { delivery_epoch: 'epoch', delivery_id: id } })
      await vi.advanceTimersByTimeAsync(5_100)
      expect(reconcile).toHaveBeenCalledOnce()
      expect(beta).toHaveBeenCalledOnce()
      expect(flow.diagnostics).toMatchObject({ pendingFrames: 1, unownedFrames: 1, ackDeliveryId: 1, queuedRecoveryKeys: 0 })
      expect(control).toHaveBeenCalledWith('transport.flow.update', {
        delivery_epoch: 'epoch', ack_delivery_id: 1,
      })
      expect(h.calls.filter(call => call.method === SNAPSHOT_RELEASE)).toHaveLength(1)
      expect(h.calls.filter(call => call.method === SNAPSHOT_READ)).toHaveLength(1)
      // Explicit retry replaces the read admission. Only a new snapshot with
      // a validated install proof can cover the suspended unowned receipt.
      const oldVersion = '1:0:1'
      flow.snapshotInstalled('alpha', oldVersion)
      expect(flow.diagnostics.ackDeliveryId).toBe(1)
      h.requestMock.mockImplementation(originalRequest)
      await lease.close()
      hub.prepareReadRetirement('alpha')
      lease = createV4SessionReadPort(rpc).open(openRequest(undefined, false))
      await (await lease.live).confirmInstalled!()
      await vi.advanceTimersByTimeAsync(50)
      expect(flow.diagnostics).toMatchObject({ pendingFrames: 0, unownedFrames: 0, ackDeliveryId: 2 })
    } finally {
      flow.close()
      hub.dispose()
      client.disconnect()
      await lease.close()
      vi.useRealTimers()
    }
  })

  for (const source of ['initial', 'reconciliation'] as const) {
    it.each([
      ['SNAPSHOT_STALE', 'unavailable', true],
      ['SNAPSHOT_EXPIRED', 'unavailable', true],
      ['SNAPSHOT_BUSY', 'busy', false],
      ['RPC_TIMEOUT', 'timeout', false],
    ] as const)(`${source} installation recovers after %s on the current lease`, async (code, kind, replace) => {
      const h = installationHarness()
      const lease = createV4SessionReadPort(h.rpc).open(openRequest())
      try {
        let live = await lease.live
        if (source === 'reconciliation') {
          await live.confirmInstalled!()
          live = await lease.reconcile()
        }
        const before = h.calls.filter(call => call.method === SNAPSHOT_READ)
        const revision = before[before.length - 1]!.params!.sync_revision
        const error = Object.assign(new Error('Snapshot installation is no longer available'), {
          code, retryable: false, retry_after_ms: 100,
        })
        h.resume.mockRejectedValueOnce(error)
        await expect(live.confirmInstalled!()).rejects.toMatchObject({
          name: 'SessionReadFailure', kind, retryable: true, retryAfterMs: 100, cause: error,
        })

        const [first, concurrent] = await Promise.all([lease.reconcile(), lease.reconcile()])
        expect(first).toBe(concurrent)
        await first.confirmInstalled!()
        const reads = h.calls.filter(call => call.method === SNAPSHOT_READ)
        expect(reads).toHaveLength(before.length + Number(replace))
        expect(reads[reads.length - 1]!.params!.sync_revision === revision).toBe(!replace)
        expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(1)
        expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(0)
      } finally { await lease.close() }
    })
  }

  it.each(['legacy-flow', 'consumption'] as const)('maps %s confirmation failures at the same boundary', async source => {
    const h = installationHarness(source !== 'legacy-flow')
    const lease = createV4SessionReadPort(h.rpc).open(openRequest())
    try {
      const live = await lease.live
      const error = Object.assign(new Error('confirmation busy'), { code: 'STORAGE_BUSY' })
      const confirmation = source === 'legacy-flow' ? h.resumeFlow : h.consume
      confirmation.mockRejectedValueOnce(error)
      await expect(live.confirmInstalled!()).rejects.toMatchObject({ kind: 'busy', retryable: true, cause: error })
    } finally { await lease.close() }
  })

  it.each(['abort', 'generation'] as const)('fences confirmation and its synchronous check after %s', async reason => {
    const h = installationHarness()
    const controller = new AbortController()
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(controller.signal))
    try {
      const live = await lease.live
      if (reason === 'abort') controller.abort()
      else h.setGeneration(8)
      await expect(live.confirmInstalled!()).rejects.toMatchObject({ kind: 'aborted' })
      expect(() => live.assertInstalledCurrent!()).toThrow(SessionReadFailure)
      expect(h.resume).not.toHaveBeenCalled()
    } finally { await lease.close() }
  })

  it.each(['abort', 'generation'] as const)('rejects an in-flight proof after %s without consuming its tail', async reason => {
    const h = installationHarness()
    const controller = new AbortController()
    const lease = createV4SessionReadPort(h.rpc).open(openRequest(controller.signal))
    try {
      const live = await lease.live
      const proof = deferred<Awaited<ReturnType<typeof h.resume>>>()
      h.resume.mockImplementationOnce(() => proof.promise)
      const rejected = expect(live.confirmInstalled!()).rejects.toMatchObject({ kind: 'aborted' })
      await flushAsyncWork()
      if (reason === 'abort') controller.abort()
      else h.setGeneration(8)
      proof.resolve({ ...h.resume.mock.calls[0]![0], session_id: null, session_epoch: 3, replay_to_seq: 8 })
      await rejected
      expect(h.consume).not.toHaveBeenCalled()
    } finally { await lease.close() }
  })

  it('preserves terminal failure and contract-error semantics', async () => {
    const h = installationHarness()
    const lease = createV4SessionReadPort(h.rpc).open(openRequest())
    try {
      const live = await lease.live
      h.resume.mockRejectedValueOnce(Object.assign(new Error('not authorized'), {
        code: 'UNAUTHORIZED', retryable: false,
      }))
      await expect(live.confirmInstalled!()).rejects.toMatchObject({ kind: 'unavailable', retryable: false })
      const contractError = new SessionReadContractError('invalid installation proof')
      h.resume.mockRejectedValueOnce(contractError)
      await expect(live.confirmInstalled!()).rejects.toBe(contractError)
    } finally { await lease.close() }
  })

  it('keeps old installation closures bound to the retired snapshot', async () => {
    const h = installationHarness()
    const lease = createV4SessionReadPort(h.rpc).open(openRequest())
    try {
      const old = await lease.live
      await old.confirmInstalled!()
      const current = await lease.reconcile()
      const resumeCount = h.resume.mock.calls.length
      await expect(old.confirmInstalled!()).rejects.toMatchObject({ kind: 'aborted' })
      expect(() => old.assertInstalledCurrent!()).toThrow(SessionReadFailure)
      expect(h.resume).toHaveBeenCalledTimes(resumeCount)
      await current.confirmInstalled!()
      expect(h.resume).toHaveBeenCalledTimes(resumeCount + 1)
      expect(h.calls.filter(call => call.method === SNAPSHOT_RELEASE)).toHaveLength(1)
    } finally { await lease.close() }
  })

  it('hydrates from the recovered subscription ACK after the original subscribe failed', async () => {
    const h = installationHarness()
    h.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, new RpcTimeoutError(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, 7_000))
    const lease = createV4SessionReadPort(h.rpc).open(openRequest())
    try {
      await Promise.all([
        expect(lease.live).rejects.toMatchObject({ kind: 'timeout' }),
        expect(lease.metadata).rejects.toMatchObject({ kind: 'timeout' }),
      ])
      h.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribeResult({
        ...metadataFields(false), current_stream_seq: 25,
      }))
      await (await lease.reconcile()).confirmInstalled!()
      const [first, concurrent] = await Promise.all([lease.retryMetadata(), lease.retryMetadata()])
      expect(first).toBe(concurrent)
      expect(first).toMatchObject({
        hydrationComplete: true,
        pendingUserInputsCursor: { streamGeneration: 'stream-1', currentStreamSeq: 25 },
      })
      expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_HYDRATE_METHOD)).toHaveLength(1)
      expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(2)
      expect(h.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(0)
    } finally { await lease.close() }
  })
})

describe('v4 SessionReadPort Adapter', () => {
  it('recovers admission on the same lease after ready times out without a socket generation change', async () => {
    const harness = makeHarness()
    harness.rpc.ready.mockRejectedValueOnce(new RpcTimeoutError('ready', 15_000))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    // All initial consumers see the bounded failure, rather than hiding it.
    await Promise.all([
      expect(lease.live).rejects.toMatchObject({ kind: 'timeout' }),
      expect(lease.metadata).rejects.toMatchObject({ kind: 'timeout' }),
      expect(lease.criticalRequestsQueued).rejects.toMatchObject({ kind: 'timeout' }),
    ])
    expect(harness.calls).toHaveLength(0)

    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ task_id: null, events: [] }))
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, hydrateResult({
      run_status: 'idle', active_task: null,
      last_task: { task_id: 'task-1', status: 'succeeded' },
    }))
    const [first, second] = await Promise.all([lease.reconcile(), lease.reconcile()])
    expect(first).toBe(second)
    expect(first.initialMetadata.runStatus).toBe('idle')
    expect(harness.rpc.ready).toHaveBeenCalledTimes(2)
    expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(1)
    await expect(lease.readHistory({ direction: 'latest', limit: 100,
      signal: new AbortController().signal })).resolves.toMatchObject({ loadedCount: 1 })
    await lease.close()
  })

  it('refreshes a live subscription in place and coalesces concurrent reconciliation', async () => {
    const harness = makeHarness()
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    harness.calls.length = 0
    const fresh = deferred<SessionsMessagesSnapshotResult>()
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, fresh.promise)
    const first = lease.reconcile()
    const second = lease.reconcile()
    await flushAsyncWork()
    expect(harness.calls.map(call => call.method)).toEqual([SESSIONS_MESSAGES_SNAPSHOT_METHOD])
    fresh.resolve(snapshotResult({ current_stream_seq: 42 }))
    const [a, b] = await Promise.all([first, second])
    expect(a).toBe(b)
    expect(a.snapshotCursor?.currentStreamSeq).toBe(42)
    expect(a.initialMetadata.pendingUserInputsCursor).toEqual({
      streamGeneration: 'stream-1', currentStreamSeq: 42,
    })
    expect(a.initialMetadata.goalSnapshotStreamSeq).toBe(6)
    expect(harness.calls.map(call => call.method)).toEqual([
      SESSIONS_MESSAGES_SNAPSHOT_METHOD, SESSIONS_MESSAGES_HYDRATE_METHOD,
    ])
    await lease.close()
  })

  it('retains a newer live questionnaire across late empty in-place hydration, then expires it at a newer snapshot', async () => {
    const harness = makeHarness()
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    const scope = effectScope()
    const events = createConversationEventsTestHarness()
    const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
    const approvals = scope.run(() => useChatApprovals({
      gatewayAvailability: ref('available'),
      approvalCenter: {
        snapshot: vi.fn(async () => ({ pending: [], mode: 'prompt' as const })),
        subscribe: vi.fn(() => ({ close: vi.fn() })),
        subscribeAvailability: vi.fn(() => ({ close: vi.fn() })),
      } as never,
      conversationEvents: events.events,
      clarificationSubmission: { submit: vi.fn() } as never,
      sessionKey: ref('alpha'), interruptState,
      runStatus: ref({ status: 'running', label: '', task: { task_id: 'task-1' } }),
      stream: { isStreaming: ref(true), appendInterruptFrame: vi.fn(), ensureInterruptBubble: vi.fn() },
    }))!
    const unsubscribe = approvals.subscribe()
    const applyMetadata = (metadata: SessionReadMetadata) => approvals.applyUserInputBootstrap({
      sessionKey: metadata.sessionKey,
      epoch: metadata.epoch,
      streamSeq: metadata.pendingUserInputsCursor?.currentStreamSeq,
      streamGeneration: metadata.pendingUserInputsCursor?.streamGeneration,
      pendingUserInputs: [...metadata.pendingUserInputs],
    })
    const ask = (requestId: string, streamSeq: number) => events.emit({
      kind: 'conversation', event: {
        kind: 'known', semanticKind: 'tool-result',
        payload: {
          key: 'alpha', task_id: 'task-1', epoch: 3,
          stream_generation: 'stream-1', stream_seq: streamSeq,
          id: `call-${requestId}`, name: 'request_user_input',
          approvalResult: {
            kind: 'user_input', paused: true, request_id: requestId,
            run_id: 'task-1', step: 'clarify',
            clarify_schema: {
              presentation: 'plan_questionnaire_v1',
              fields: [{ name: 'scope', type: 'string', required: true, prompt: 'Which scope?' }],
            },
          },
        },
        meta: {}, sessionKey: 'alpha', taskId: 'task-1', turnId: null,
        streamGeneration: 'stream-1', streamSeq, connectionSeq: null, generationEpoch: null,
      },
    } as ConversationEvent)
    try {
      ask('older-question', 10)
      harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ current_stream_seq: 40 }))
      const lateMetadata = deferred<SessionsMessagesHydrateResult>()
      harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, lateMetadata.promise)
      const recovery = lease.reconcile()
      await flushAsyncWork()
      expect(harness.calls[harness.calls.length - 1]?.method).toBe(SESSIONS_MESSAGES_HYDRATE_METHOD)

      ask('newer-question', 41)
      lateMetadata.resolve(hydrateResult({ pendingUserInputs: [], goalSnapshotStreamSeq: 77 }))
      const recovered = await recovery
      expect(recovered.initialMetadata.pendingUserInputsCursor).toEqual({
        streamGeneration: 'stream-1', currentStreamSeq: 40,
      })
      expect(recovered.initialMetadata.goalSnapshotStreamSeq).toBe(77)
      applyMetadata(recovered.initialMetadata)
      expect(approvals.pendingClarify.value?.requestId).toBe('newer-question')
      expect(interruptState.value.get('older-question')?.resolution).toBe('expired')
      expect(interruptState.value.get('newer-question')?.resolution).not.toBe('expired')

      harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ current_stream_seq: 42 }))
      harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, hydrateResult({ pendingUserInputs: [] }))
      applyMetadata((await lease.reconcile()).initialMetadata)
      expect(approvals.pendingClarify.value).toBeNull()
      expect(interruptState.value.get('newer-question')?.resolution).toBe('expired')
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(1)
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(0)
    } finally {
      unsubscribe()
      scope.stop()
      await lease.close()
    }
  })

  it('retains the confirmed subscription lower bound when an older Gateway has no snapshot capability', async () => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, Object.assign(new Error('legacy Gateway'), {
      code: 'METHOD_NOT_FOUND',
    }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    try {
      const recovered = await lease.reconcile()
      expect(recovered.snapshot).toBeNull()
      expect(recovered.initialMetadata.pendingUserInputsCursor).toEqual({
        streamGeneration: 'stream-1', currentStreamSeq: 9,
      })
      expect(recovered.initialMetadata.goalSnapshotStreamSeq).toBe(6)
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(1)
    } finally {
      await lease.close()
    }
  })

  it('rejects original-lease reconciliation after a real connection generation change', async () => {
    const harness = makeHarness()
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    harness.calls.length = 0
    harness.setGeneration(8)
    await expect(lease.reconcile()).rejects.toMatchObject({ kind: 'aborted' })
    expect(harness.calls).toHaveLength(0)
  })

  it('restores physical provider models independently of the router selection', async () => {
    const harness = makeHarness()
    const routerDecision = { model: 'deepseek-v4-pro', tier: 'c2', decision_id: 'decision-A' }
    const activities = [
      { phase: 'requesting', model: 'deepseek-v4-pro' },
      { phase: 'fallback', model: 'kimi-k2.7-code' },
      { phase: 'retrying', model: 'kimi-k2.7-code', retry_attempt: 1 },
      { phase: 'fallback', model: 'deepseek-v4-pro-0813' },
      { phase: 'reasoning', model: 'deepseek-v4-pro-0813' },
      { phase: 'reasoning', heartbeat: true },
    ]
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ events: [
      { event: 'session.event.router_decision', payload: routerDecision },
      ...activities.map(payload => ({ event: 'session.event.provider_activity', payload })),
    ] }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    try {
      const live = await lease.live
      expect(live.snapshot?.events).toEqual([
        { semanticKind: 'router-decision', payload: routerDecision },
        ...activities.map(payload => ({ semanticKind: 'provider-activity', payload })),
      ])
    } finally {
      await lease.close()
    }
  })

  it.each([
    {
      name: 'an empty canonical key',
      wire: { key: '', sessionKey: 'alpha', stream_seq: 1 },
      expected: { key: 'alpha', stream_seq: 1 },
    },
    {
      name: 'conflicting legacy spellings',
      wire: { key: 'alpha', sessionKey: 'other', stream_seq: 1, streamSeq: 2 },
      expected: { key: 'alpha', stream_seq: 1 },
    },
    {
      name: 'finite fractional cursors',
      wire: { key: 'alpha', epoch: 1.5, stream_seq: 1.5 },
      expected: { key: 'alpha', epoch: 1.5, stream_seq: 1.5 },
    },
    {
      name: 'finite negative cursors',
      wire: { key: 'alpha', epoch: -1, stream_seq: -1 },
      expected: { key: 'alpha', epoch: -1, stream_seq: -1 },
    },
    {
      name: 'a correctly typed legacy fallback',
      wire: { key: 7, sessionKey: 'alpha', stream_seq: 'bad', streamSeq: 1 },
      expected: { key: 'alpha', stream_seq: 1 },
    },
  ])('preserves snapshot compatibility for $name', async ({ wire, expected }) => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ events: [{
      event: 'session.event.text_delta',
      payload: { ...wire, text: 'visible content' },
    }] }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    try {
      const live = await lease.live
      expect(live.snapshot?.events).toEqual([{
        semanticKind: 'text-delta',
        payload: { ...expected, text: 'visible content' },
      }])
    } finally {
      await lease.close()
    }
  })

  it.each([
    { event: 'session.event.turn_committed', invalid: { schema_version: 2 } },
    { event: 'session.event.turn_committed', invalid: { stream_seq: -1 } },
    { event: 'session.event.turn_committed', invalid: { stream_seq: 1.5 } },
    { event: 'session.event.turn_committed', invalid: { client_message_id: null } },
    { event: 'session.event.turn_committed', invalid: { taskId: 'conflicting-task' } },
  ])('rejects invalid external snapshot receipts through the event boundary: $invalid', async ({ event, invalid }) => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ events: [{ event, payload: {
      schema_version: 1, session_key: 'alpha', task_id: 'task-1', turn_id: 'turn-1',
      status: 'succeeded', terminal_reason: 'completed', finished_at: 123, stream_seq: 2,
      ...invalid,
    } }] }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await expect(lease.live).rejects.toThrow()
    await lease.close()
  })

  it('projects snapshot identity, terminal proof and authoritative empty resets once', async () => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshotResult({ events: [
      { event: 'session.event.error', payload: {
        key: 'alpha', activeTask: { taskId: 'task-1', status: 'failed' },
        streamGeneration: 'stream-1', streamSeq: 2, code: 'usage_accounting_busy',
        usage_call_index: 1, no_prior_provider_dispatch: true, replay_safe: true,
        user_message_id: 'user-1', turn_outcome: { kind: 'blocked', reason: 'usage_accounting_busy' },
      } },
      { event: 'session.event.answer_generation_reset', payload: {
        key: 'alpha', task_id: 'task-1', old_generation_epoch: 1, new_generation_epoch: 2,
        authoritative_text_snapshot: '', authoritativeTextSnapshot: 'stale',
      } },
      { event: 'session.event.future_snapshot_event', payload: { key: 'alpha', status: 'failed' } },
    ] }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    const live = await lease.live
    expect(live.snapshot?.events).toHaveLength(2)
    expect(live.snapshot?.events[0]).toMatchObject({ semanticKind: 'turn-failed', payload: {
      key: 'alpha', task_id: 'task-1', stream_generation: 'stream-1', stream_seq: 2,
      terminalOutcome: { turnId: 'task-1', replaySafe: true, noPriorProviderDispatch: true, userMessageId: 'user-1' },
    } })
    expect(live.snapshot?.events[1]).toMatchObject({ semanticKind: 'answer-generation-reset', payload: { authoritative_text_snapshot: '' } })
    await lease.close()
  })

  it('preserves the established retryability of coded and uncoded failures', () => {
    expect(mapSessionReadError(Object.assign(new Error('invalid'), {
      code: 'INVALID_REQUEST',
    }))).toMatchObject({
      kind: 'unavailable',
      retryable: false,
    } satisfies Partial<SessionReadFailure>)
    expect(mapSessionReadError(new Error('connection recycled'))).toMatchObject({
      kind: 'unavailable',
      retryable: true,
    } satisfies Partial<SessionReadFailure>)
  })

  it.each([
    ['HISTORY_CURSOR_INVALID', 'invalid'],
    ['history_cursor_invalidated', 'stale'],
  ] as const)('maps %s to reload-latest cursor recovery', (code, reason) => {
    const cause = Object.assign(new Error('cursor rejected'), { code })

    expect(mapSessionReadError(cause)).toMatchObject({
      name: 'SessionReadHistoryCursorError',
      code: 'history-cursor-rejected',
      reason,
      recovery: 'reload-latest',
      cause,
    } satisfies Partial<SessionReadHistoryCursorError>)
  })

  it('queues critical frames in order while live, metadata and history settle independently', async () => {
    const harness = makeHarness()
    const subscribe = deferred<SessionsMessagesSubscribeResult>()
    const snapshot = deferred<SessionsMessagesSnapshotResult>()
    const history = deferred<ChatHistoryResult>()
    const hydrated = deferred<SessionsMessagesHydrateResult>()
    harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribe.promise)
    harness.results.set(SESSIONS_MESSAGES_SNAPSHOT_METHOD, snapshot.promise)
    harness.results.set(CHAT_HISTORY_METHOD, history.promise)
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, hydrated.promise)
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())

    await flushAsyncWork()
    await expect(lease.criticalRequestsQueued).resolves.toBeUndefined()
    expect(harness.calls.map(call => call.method)).toEqual([
      SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
      SESSIONS_MESSAGES_SNAPSHOT_METHOD,
      CHAT_HISTORY_METHOD,
    ])

    subscribe.resolve(subscribeResult({
      ...metadataFields(false),
      hydration_complete: false,
    }))
    snapshot.resolve(snapshotResult())
    await flushAsyncWork()
    expect(harness.calls.map(call => call.method)).toContain(SESSIONS_MESSAGES_HYDRATE_METHOD)

    const live = await lease.live
    expect(live).toMatchObject({
      sessionKey: 'alpha',
      activity: 'foreground',
      activeTaskId: 'task-snapshot',
      initialMetadata: {
        hydrationComplete: false,
        pendingUserInputsCursor: { streamGeneration: 'stream-1', currentStreamSeq: 9 },
        projectWorkspace: { display_name: 'Workspace One' },
      },
      snapshot: {
        sessionKey: 'alpha',
        events: [{
          semanticKind: 'text-delta',
          payload: { task_id: 'task-snapshot', text: 'hello' },
        }],
      },
      cursor: {
        streamGeneration: 'stream-1',
        currentStreamSeq: 9,
      },
      snapshotCursor: {
        streamGeneration: 'stream-1',
        currentStreamSeq: 8,
      },
    })
    const snapshotPayload = live.snapshot?.events[0]?.payload
    expect(snapshotPayload).toMatchObject({
      input: { snake_value: true },
    })
    expect(Object.isFrozen(snapshotPayload)).toBe(true)
    expect(Object.isFrozen(snapshotPayload?.input)).toBe(true)
    let metadataSettled = false
    let historySettled = false
    void lease.metadata.finally(() => { metadataSettled = true })
    const firstHistory = lease.readHistory({
      direction: 'latest',
      limit: 100,
      signal: openRequest().signal,
    }).finally(() => { historySettled = true })
    await flushAsyncWork()
    expect(metadataSettled).toBe(false)
    expect(historySettled).toBe(false)

    hydrated.resolve(hydrateResult())
    history.resolve(historyResult())
    await expect(lease.metadata).resolves.toMatchObject({
      hydrationComplete: true,
      pendingUserInputsCursor: { streamGeneration: 'stream-1', currentStreamSeq: 9 },
    })
    await expect(firstHistory).resolves.toMatchObject({ loadedCount: 1 })

    await lease.close()
  })

  it('maps known fields while preserving opaque and additive JSON keys', async () => {
    const harness = makeHarness()
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    const projectedMetadata = await lease.metadata
    const latest = await lease.readHistory({
      direction: 'latest',
      limit: 100,
      signal: openRequest().signal,
    })

    expect(projectedMetadata).toMatchObject({
      sessionKey: 'alpha',
      workspaceId: 'workspace-1',
      projectWorkspace: {
        display_name: 'Workspace One',
        nested_context: { snake_value: true },
      },
      activeTaskGroupIds: [],
      runModeLock: { locked: true, runMode: 'safe', source: 'profile' },
      pendingUserInputs: [{ request_id: 'input-1' }],
      collaboration: { mode_name: 'delegate' },
      planPresentations: [{ revisionId: 'plan-1', dismissed: true, stateRevision: 2 }],
      currentPlan: { plan_id: 'plan-1' },
      activePlanRun: { run_id: 'run-1' },
      goal: { goal_id: 'goal-1' },
      tasks: [{ task_id: 'task-1' }],
      activeTask: { task_id: 'task-1' },
      lastTask: { task_id: 'task-0' },
      queuedTaskIds: ['task-2'],
      epoch: 3,
      pendingUserInputsCursor: { streamGeneration: 'stream-1', currentStreamSeq: 9 },
      hydrationComplete: true,
      additional: { future_metadata: { snake_value: true } },
    })
    expect(latest).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-1',
      newestCursor: 'cursor-9',
      scope: 'latestWindow',
      loadedCount: 1,
      pageSize: 100,
      canonicalAvailable: false,
      canonicalComplete: true,
      additional: { future_history_field: { nested_value: true } },
      messages: [{
        id: '41',
        messageId: 'message-1',
        transcriptId: '42',
        role: 'assistant',
        text: 'hello',
        createdAt: 1_725_199_200,
        reasoningContent: '  thinking exactly  ',
        routerDecision: { tier: 'c1' },
        artifacts: [{ artifact_id: 'artifact-1' }],
        toolCalls: [{ tool_name: 'read' }],
        timeline: [{ segment_kind: 'thinking' }],
        attachments: [{ attachment_id: 'attachment-1' }],
        promptAnnotations: [{ annotation_kind: 'cache' }],
        turnContext: {
          turnId: 'turn-1',
          promotedTurnId: 'turn-promoted',
          appliedIteration: 2,
          activityMarkers: [{ marker_id: 'marker-1' }],
          additional: { run_mode: 'safe' },
        },
        usage: { total_tokens: 8 },
        model: 'model-1',
        inputTokens: 3,
        outputTokens: 5,
        provenance: {
          kind: 'forwarded',
          sourceSessionKey: 'source-session',
          sourceTool: 'delegate',
        },
        additional: { additive_message_field: { nested_value: true } },
      }],
      compactionSummaries: [{
        id: '7',
        compactionId: 'compact-1',
        compactionIndex: 2,
        coveredThroughId: '40',
        additional: { future_summary_field: 'kept' },
      }],
      turnOutcomes: [{
        turnId: 'turn-1',
        outcome: { finish_reason: 'stop' },
        errorClass: 'usage_accounting_busy',
        retryable: true,
        activitySnapshot: { task_id: 'task-1', phase_name: 'finalize' },
        usage: { input_tokens: 3, output_tokens: 5 },
        replayProof: {
          usageCallIndex: 1,
          noPriorProviderDispatch: true,
          replaySafe: true,
          retryAfterMs: 100,
          userMessageId: 'message-user-1',
          terminalMessage: 'retry safely',
        },
        additional: { future_outcome_field: true },
      }],
    })
    expect(Object.isFrozen(projectedMetadata.projectWorkspace)).toBe(true)
    expect(Object.isFrozen(projectedMetadata.projectWorkspace?.nested_context)).toBe(true)
    expect(Object.isFrozen(projectedMetadata.additional)).toBe(true)
    expect(Object.isFrozen(projectedMetadata.additional.future_metadata)).toBe(true)

    await lease.readHistory({
      direction: 'before',
      cursor: 'older',
      limit: 25,
      signal: openRequest().signal,
    })
    await lease.readHistory({
      direction: 'after',
      cursor: 'newer',
      limit: 10,
      signal: openRequest().signal,
    })
    const historyCalls = harness.calls.filter(call => call.method === CHAT_HISTORY_METHOD)
    expect(historyCalls[1]?.params).toMatchObject({ before: 'older', limit: 25 })
    expect(historyCalls[1]?.params).not.toHaveProperty('after')
    expect(historyCalls[2]?.params).toMatchObject({ after: 'newer', limit: 10 })
    expect(historyCalls[2]?.params).not.toHaveProperty('before')

    await lease.close()
  })

  it('hydrates and retries metadata without reopening live frames', async () => {
    const harness = makeHarness()
    harness.results.set(
      SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
      subscribeResult({ ...metadataFields(false), hydration_complete: false }),
    )
    harness.results.set(
      SESSIONS_MESSAGES_HYDRATE_METHOD,
      Object.assign(new Error('hydrate failed'), { code: 'STORAGE_BUSY' }),
    )
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())

    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })
    await expect(lease.metadata).rejects.toThrow('hydrate failed')
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, hydrateResult({
      routing: { mode: 'manual' },
    }))
    const firstRetry = lease.retryMetadata()
    const secondRetry = lease.retryMetadata()
    // Hydration has no stream cursor of its own. Retain the subscription's
    // lower bound so an empty pending list cannot erase newer live questions.
    const expectedMetadata = {
      routing: { mode: 'manual' },
      pendingUserInputsCursor: { streamGeneration: 'stream-1', currentStreamSeq: 9 },
    }
    await expect(firstRetry).resolves.toMatchObject(expectedMetadata)
    await expect(secondRetry).resolves.toMatchObject(expectedMetadata)
    expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_HYDRATE_METHOD))
      .toHaveLength(2)
    expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD))
      .toHaveLength(1)

    await lease.close()
  })

  it('falls back only for a pre-send missing snapshot capability', async () => {
    const missing = makeHarness()
    missing.requestMock.mockImplementation((
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<unknown> => {
      missing.calls.push({ method, params, options })
      if (method === SESSIONS_MESSAGES_SNAPSHOT_METHOD) {
        return Promise.reject(Object.assign(new Error('missing'), { code: 'METHOD_NOT_FOUND' }))
      }
      options?.onSent?.(missing.rpc.generation)
      return Promise.resolve(missing.results.get(method))
    })
    const missingLease = createV4SessionReadPort(missing.rpc).open(openRequest())
    await expect(missingLease.criticalRequestsQueued).resolves.toBeUndefined()
    await expect(missingLease.live).resolves.toMatchObject({ snapshot: null })
    await missingLease.close()

    const failed = makeHarness()
    failed.requestMock.mockImplementation((
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<unknown> => {
      failed.calls.push({ method, params, options })
      if (method === SESSIONS_MESSAGES_SNAPSHOT_METHOD) {
        return Promise.reject(Object.assign(new Error('timeout'), { code: 'TIMEOUT' }))
      }
      options?.onSent?.(failed.rpc.generation)
      return Promise.resolve(failed.results.get(method))
    })
    const failedLease = createV4SessionReadPort(failed.rpc).open(openRequest())
    await expect(failedLease.live).rejects.toThrow('timeout')
    await expect(failedLease.criticalRequestsQueued).rejects.toThrow('timeout')
    await failedLease.close()
  })

  it('projects a missing subscribe as a domain failure without queuing eager history', async () => {
    const harness = makeHarness()
    harness.requestMock.mockImplementation((
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<unknown> => {
      harness.calls.push({ method, params, options })
      if (method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD) {
        return Promise.reject(Object.assign(new Error('session missing'), {
          code: 'SESSION_NOT_FOUND',
        }))
      }
      options?.onSent?.(harness.rpc.generation)
      return Promise.resolve(harness.results.get(method))
    })
    const request = openRequest()
    const lease = createV4SessionReadPort(harness.rpc).open(request)
    const live = expect(lease.live)
      .rejects.toBeInstanceOf(SessionReadSessionMissingError)
    const metadata = expect(lease.metadata)
      .rejects.toBeInstanceOf(SessionReadSessionMissingError)
    const admitted = expect(lease.criticalRequestsQueued)
      .rejects.toBeInstanceOf(SessionReadSessionMissingError)
    const history = expect(lease.readHistory({
      direction: 'latest',
      limit: 100,
      signal: request.signal,
    })).rejects.toBeInstanceOf(SessionReadSessionMissingError)

    await Promise.all([live, metadata, admitted, history])
    expect(harness.calls.filter(call => call.method === CHAT_HISTORY_METHOD)).toEqual([])

    await lease.close()
  })

  it('isolates malformed history and hydration from a healthy live subscription', async () => {
    const harness = makeHarness()
    harness.results.set(
      SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
      subscribeResult({ ...metadataFields(false), hydration_complete: false }),
    )
    harness.results.set(CHAT_HISTORY_METHOD, { messages: [] })
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, { key: 'alpha' })
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())

    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })
    await expect(lease.readHistory({
      direction: 'latest',
      limit: 100,
      signal: openRequest().signal,
    })).rejects.toBeInstanceOf(SessionReadContractError)
    await expect(lease.metadata).rejects.toBeInstanceOf(SessionReadContractError)

    await lease.close()
  })

  it.each(['subscribe', 'hydrate'] as const)(
    'decodes only the v3 trusted run-mode alias at the %s boundary',
    async (boundary) => {
      const harness = makeHarness()
      const result = boundary === 'subscribe' ? subscribeResult() : hydrateResult()
      const wire = {
        ...result,
        run_mode_lock: { locked: true, runMode: 'trusted', source: 'task', extra: 'preserved' },
        future_metadata: { runMode: 'trusted' },
      }
      const original = structuredClone(wire)
      harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, boundary === 'subscribe'
        ? wire : subscribeResult({ ...metadataFields(false), hydration_complete: false }))
      harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, wire)
      const lease = createV4SessionReadPort(harness.rpc).open(openRequest())

      await Promise.all([
        expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' }),
        expect(lease.metadata).resolves.toMatchObject({
          runModeLock: { locked: true, runMode: 'safe', source: 'task', additional: { extra: 'preserved' } },
          additional: { future_metadata: { runMode: 'trusted' } },
        }),
      ])
      expect(wire).toEqual(original)
      await lease.close()
    },
  )

  it.each(['trusted', 'unknown'])(
    'validates run mode %s when reconciliation retries a lost subscribe ACK', async (runMode) => {
      const harness = makeHarness()
      harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, new Error('subscribe ACK lost'))
      const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
      await Promise.all([
        expect(lease.live).rejects.toThrow('subscribe ACK lost'),
        expect(lease.metadata).rejects.toThrow('subscribe ACK lost'),
      ])
      harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, {
        ...subscribeResult(), run_mode_lock: { locked: true, runMode },
      })
      harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, {
        ...hydrateResult(), run_mode_lock: { locked: true, runMode: 'trusted' },
      })
      if (runMode === 'trusted') {
        await expect(lease.reconcile()).resolves.toMatchObject({
          initialMetadata: { runModeLock: { locked: true, runMode: 'safe' } },
        })
      } else {
        await expect(lease.reconcile()).rejects.toBeInstanceOf(SessionReadContractError)
      }
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(2)
      await lease.close()
    },
  )

  it.each(['safe', 'full', undefined] as const)('preserves canonical or absent run mode %s', async (runMode) => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, {
      ...hydrateResult(), run_mode_lock: { locked: runMode !== undefined, ...(runMode ? { runMode } : {}) },
    })
    harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
      subscribeResult({ ...metadataFields(false), hydration_complete: false }))
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    await expect(lease.metadata).resolves.toMatchObject({
      runModeLock: { locked: runMode !== undefined, runMode: runMode ?? null },
    })
    await lease.close()
  })

  it.each([
    { locked: true, runMode: 'bypass' },
    { locked: true, runMode: 'unknown' },
    { locked: true, runMode: null },
    { locked: 'yes', runMode: 'trusted' },
    { locked: true, runMode: 'trusted', source: 42 },
  ])('keeps malformed run-mode locks invalid: %j', async (lock) => {
    const harness = makeHarness()
    harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
      subscribeResult({ ...metadataFields(false), hydration_complete: false }))
    harness.results.set(SESSIONS_MESSAGES_HYDRATE_METHOD, { ...hydrateResult(), run_mode_lock: lock })
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    await lease.live
    await expect(lease.metadata).rejects.toBeInstanceOf(SessionReadContractError)
    await lease.close()
  })

  it('normalizes only the legacy canonical proof fields before result validation', async () => {
    const harness = makeHarness()
    const legacy = { ...historyResult() } as Record<string, unknown>
    delete legacy.canonical_available
    delete legacy.canonical_complete
    harness.results.set(CHAT_HISTORY_METHOD, legacy)
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest(
      new AbortController().signal,
      false,
    ))

    await lease.live
    await expect(lease.readHistory({
      direction: 'latest',
      limit: 100,
      signal: new AbortController().signal,
    })).resolves.toMatchObject({
      canonicalAvailable: null,
      canonicalComplete: null,
    })

    harness.results.set(CHAT_HISTORY_METHOD, {
      ...legacy,
      canonical_available: false,
    })
    await expect(lease.readHistory({
      direction: 'latest',
      limit: 20,
      signal: new AbortController().signal,
    })).resolves.toMatchObject({
      canonicalAvailable: false,
      canonicalComplete: null,
    })

    const malformed = { ...legacy, loaded_count: 'one' }
    harness.results.set(CHAT_HISTORY_METHOD, malformed)
    await expect(lease.readHistory({
      direction: 'latest',
      limit: 20,
      signal: new AbortController().signal,
    })).rejects.toBeInstanceOf(SessionReadContractError)

    await lease.close()
  })

  it('derives history transport timeout policy from the injected concurrent-read capability', async () => {
    for (const [concurrent, expectedAction] of [
      [true, 'reject'],
      [false, 'reconnect'],
    ] as const) {
      const harness = makeHarness()
      const signal = new AbortController().signal
      const lease = createV4SessionReadPort(harness.rpc, {
        concurrentHistoryReads: () => concurrent,
        now: () => 10_000,
      }).open(openRequest(signal, false))
      await lease.live

      await lease.readHistory({
        direction: 'before',
        cursor: 'older',
        limit: 25,
        signal,
        budgetMs: 5_000,
        deadlineAt: 11_200,
      })
      const call = harness.calls.find(candidate => candidate.method === CHAT_HISTORY_METHOD)
      expect(call?.options).toMatchObject({
        signal,
        timeoutMs: 1_200,
        timeoutAction: expectedAction,
        abortAction: 'reject',
        expectedGeneration: 7,
        recoveryClass: 'safe-read',
      })

      await lease.close()
    }
  })

  it('closes before connection admission without sending subscribe or unsubscribe', async () => {
    const harness = makeHarness()
    const ready = deferred<undefined>()
    harness.rpc.ready.mockImplementation(() => ready.promise)
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest())
    void lease.criticalRequestsQueued.catch(() => {})
    void lease.metadata.catch(() => {})

    const closing = lease.close()
    await flushAsyncWork()
    expect(harness.calls).toHaveLength(0)
    ready.resolve(undefined)

    await expect(closing).resolves.toBeUndefined()
    await expect(lease.live).rejects.toMatchObject({
      name: 'SessionReadFailure',
      kind: 'aborted',
    })
    expect(harness.calls).toHaveLength(0)
  })

  it('releases a sent subscribe generation even while its ACK is still pending', async () => {
    const harness = makeHarness()
    const subscribeAck = deferred<SessionsMessagesSubscribeResult>()
    harness.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribeAck.promise)
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest(
      new AbortController().signal,
      false,
    ))
    void lease.metadata.catch(() => {})

    await flushAsyncWork()
    await expect(lease.close()).resolves.toBeUndefined()
    expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD))
      .toHaveLength(1)

    subscribeAck.reject(new Error('subscribe ACK failed'))
    await expect(lease.live).rejects.toThrow('subscribe ACK failed')
    await lease.close()
    expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD))
      .toHaveLength(1)
  })

  it('releases a subscribe generation recorded before synchronous setup failure', async () => {
    const harness = makeHarness()
    harness.requestMock.mockImplementation((
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<unknown> => {
      harness.calls.push({ method, params, options })
      options?.onSent?.(harness.rpc.generation)
      if (method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD) {
        throw new Error('subscribe setup failed after send')
      }
      return Promise.resolve(harness.results.get(method))
    })
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest(
      new AbortController().signal,
      false,
    ))
    void lease.criticalRequestsQueued.catch(() => {})
    void lease.metadata.catch(() => {})

    await expect(lease.live).rejects.toThrow('subscribe setup failed after send')
    await expect(lease.close()).resolves.toBeUndefined()
    const releases = harness.calls.filter(
      call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD,
    )
    expect(releases).toHaveLength(1)
    expect(releases[0]?.options).toMatchObject({ expectedGeneration: 7 })
  })

  it('pins unsubscribe to the generation that physically sent subscribe', async () => {
    const same = makeHarness()
    const sameLease = createV4SessionReadPort(same.rpc).open(openRequest())
    await sameLease.live
    await sameLease.close()
    await sameLease.close()
    const releases = same.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)
    expect(releases).toHaveLength(1)
    expect(releases[0]?.params).toEqual({ key: 'alpha' })
    expect(releases[0]?.options).toMatchObject({ expectedGeneration: 7 })

    const replaced = makeHarness()
    const replacedLease = createV4SessionReadPort(replaced.rpc).open(openRequest())
    await replacedLease.live
    replaced.setGeneration(8)
    await replacedLease.close()
    expect(replaced.calls.some(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD))
      .toBe(false)
  })

  it('forwards a negotiated unsubscribe lane-retire fence to the flow ledger', async () => {
    const h = makeHarness()
    const retireLane = vi.fn<(receipt: TransportLaneRetireReceipt) => Promise<void>>().mockResolvedValue(undefined)
    h.results.set(SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD, {
      lane_retire: {
        connection_epoch: 'delivery-epoch',
        subscription_epoch: 'subscription-epoch',
        retire_token: 'retire-token',
        final_published_id: 7,
      },
    })
    const lease = createV4SessionReadPort({ ...h.rpc, retireLane }).open(openRequest())
    await lease.live
    await lease.close()
    expect(retireLane).toHaveBeenCalledWith({
      connection_epoch: 'delivery-epoch',
      subscription_epoch: 'subscription-epoch',
      retire_token: 'retire-token',
      final_published_id: 7,
    })
  })

  it('keeps a late malformed unsubscribe reply local after its successor has subscribed', async () => {
    const h = makeHarness()
    const failProtocol = vi.fn()
    const port = createV4SessionReadPort({ ...h.rpc, failProtocol })
    const prior = port.open(openRequest())
    await prior.live
    const oldReply = deferred<unknown>()
    h.results.set(SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD, oldReply.promise)
    await prior.close()
    const successor = port.open(openRequest())
    await successor.live
    oldReply.resolve({ subscribed: false })
    await flushAsyncWork()
    expect(failProtocol).not.toHaveBeenCalled()
    await expect(successor.reconcile()).resolves.toMatchObject({ sessionKey: 'alpha' })
    h.results.set(SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD, null)
    await successor.close()
  })

  it('still rejects an invalid cleanup result when no send receipt settled the close', async () => {
    const h = makeHarness()
    const original = h.requestMock.getMockImplementation()!
    h.requestMock.mockImplementation((method, params, options) => (
      method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD
        ? Promise.resolve({ subscribed: false }) : original(method, params, options)
    ))
    const lease = createV4SessionReadPort(h.rpc).open(openRequest())
    await lease.live
    await expect(lease.close()).rejects.toBeInstanceOf(SessionReadContractError)
  })
})

function deadlineHarness(options: {
  delayedAck?: boolean
  preSend?: boolean
  abortOwner?: boolean
  changeGeneration?: boolean
  snapshotError?: string
  rejectSubscribe?: boolean
  replayGap?: boolean
} = {}) {
  const base = makeHarness()
  const ack = deferred<SessionsMessagesSubscribeResult>()
  const lateSnapshot = deferred<SessionsMessagesSnapshotResult>()
  const controller = new AbortController()
  let subscribed = false
  const delivered: string[] = []
  base.results.set(SESSIONS_MESSAGES_SUBSCRIBE_METHOD, subscribeResult({
    ...(options.replayGap ? { replay_complete: false, replay_gap_reason: 'trimmed' } : {}),
  }))
  base.requestMock.mockImplementation((method, params, callOptions) => {
    base.calls.push({ method, params, options: callOptions })
    if (method === SESSIONS_MESSAGES_SNAPSHOT_METHOD && options.preSend) {
      return Promise.reject(new RpcTimeoutError(method, callOptions!.timeoutMs!))
    }
    callOptions?.onSent?.(base.rpc.generation)
    if (method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD) {
      if (options.rejectSubscribe) return Promise.reject(new Error('subscribe rejected'))
      subscribed = true
      return options.delayedAck ? ack.promise : Promise.resolve(base.results.get(method))
    }
    if (method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD) {
      subscribed = false
      return Promise.resolve(null)
    }
    if (method === SESSIONS_MESSAGES_SNAPSHOT_METHOD) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          if (options.abortOwner) controller.abort()
          if (options.changeGeneration) base.setGeneration(8)
          reject(options.snapshotError
            ? Object.assign(new Error('snapshot rejected'), { code: options.snapshotError })
            : new RpcTimeoutError(method, callOptions!.timeoutMs!))
        }, callOptions!.timeoutMs)
        lateSnapshot.promise.then(value => { clearTimeout(timer); resolve(value) }, reject)
      })
    }
    return Promise.resolve(base.results.get(method))
  })
  return {
    ...base, ack, lateSnapshot, controller,
    isSubscribed: () => subscribed,
    emit(name: string) { if (subscribed) delivered.push(name) },
    delivered,
  }
}

afterEach(() => { vi.useRealTimers() })

describe('sent snapshot deadline regression', () => {
  it.each([false, true])('retains actual subscription after 3s snapshot deadline (ACK delayed=%s)', async delayedAck => {
    vi.useFakeTimers()
    const harness = deadlineHarness({ delayedAck })
    const lifecycle = createSessionReadLifecycle({
      port: createV4SessionReadPort(harness.rpc),
      runtime: createConversationRuntime(),
      subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
    })
    const lease = lifecycle.open({ sessionKey: 'alpha', includeInitialHistory: false })
    const outcome = lease.live.then(value => ({ value }), error => ({ error }))
    try {
      await lease.criticalRequestsQueued
      const request = harness.calls.find(call => call.method === SESSIONS_MESSAGES_SNAPSHOT_METHOD)
      expect(request?.options).toMatchObject({ timeoutMs: 3000, timeoutAction: 'reject', abortAction: 'reject', expectedGeneration: 7, recoveryClass: 'safe-read' })
      await vi.advanceTimersByTimeAsync(2999)
      expect(harness.isSubscribed()).toBe(true)
      await vi.advanceTimersByTimeAsync(1)
      expect(harness.isSubscribed(), 'a request-local snapshot deadline must not unregister the acknowledged stream').toBe(true)
      await vi.advanceTimersByTimeAsync(4630)
      harness.ack.resolve(subscribeResult())
      await vi.advanceTimersByTimeAsync(0)
      const result = await outcome
      expect(result).toMatchObject({ value: { snapshot: null, sessionKey: 'alpha', activeTaskId: 'task-1' } })
      expect(lifecycle.current()).toBe(lease)
      harness.lateSnapshot.resolve(snapshotResult({ current_stream_seq: 900 }))
      await vi.advanceTimersByTimeAsync(0)
      expect(await lease.live).toMatchObject({ snapshot: null })
      // Only server-delivered tick/terminal names are synthetic; the actual
      // Port and lifecycle own the live registration and close decision.
      harness.emit('tick')
      await vi.advanceTimersByTimeAsync(600_000)
      harness.emit('session.event.done')
      harness.emit('session.event.turn_committed')
      expect(harness.delivered).toEqual(['tick', 'session.event.done', 'session.event.turn_committed'])
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_SUBSCRIBE_METHOD)).toHaveLength(1)
      expect(harness.calls.filter(call => call.method === SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)).toHaveLength(0)
    } finally { await lease.close() }
    expect(harness.isSubscribed()).toBe(false)
  })

  it('retains incomplete replay as a required history recovery without inventing a snapshot watermark', async () => {
    vi.useFakeTimers()
    const harness = deadlineHarness({ replayGap: true })
    const lifecycle = createSessionReadLifecycle({
      port: createV4SessionReadPort(harness.rpc),
      runtime: createConversationRuntime(),
      subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
    })
    const lease = lifecycle.open({ sessionKey: 'alpha', includeInitialHistory: false })
    const outcome = lease.live.then(value => ({ value }), error => ({ error }))
    try {
      await lease.criticalRequestsQueued
      await vi.advanceTimersByTimeAsync(3000)
      expect(await outcome).toMatchObject({ value: { snapshot: null, reloadRequired: 'replayGap' } })
      expect(harness.isSubscribed()).toBe(true)
      expect(await lease.history.latest()).toMatchObject({ messages: [{ id: '41', messageId: 'message-1' }] })
    } finally { await lease.close() }
  })

  it.each<{ name: string; options: NonNullable<Parameters<typeof deadlineHarness>[0]> }>([
    { name: 'not actually sent', options: { preSend: true } },
    { name: 'owner aborted', options: { abortOwner: true } },
    { name: 'generation changed', options: { changeGeneration: true } },
    { name: 'session missing', options: { snapshotError: 'SESSION_NOT_FOUND' } },
    { name: 'subscribe rejected', options: { rejectSubscribe: true } },
  ])('fails closed when $name', async ({ options }) => {
    vi.useFakeTimers()
    const harness = deadlineHarness(options)
    const lease = createV4SessionReadPort(harness.rpc).open(openRequest(harness.controller.signal, false))
    const outcome = lease.live.then(value => ({ value }), error => ({ error }))
    const admitted = lease.criticalRequestsQueued.then(() => ({ ready: true }), error => ({ error }))
    const metadata = lease.metadata.then(value => ({ value }), error => ({ error }))
    try {
      await vi.advanceTimersByTimeAsync(3000)
      expect(await outcome).toHaveProperty('error')
      if (options.preSend) expect(await admitted).toHaveProperty('error')
      if (options.rejectSubscribe) expect(await metadata).toHaveProperty('error')
    } finally { await lease.close() }
  })
})
