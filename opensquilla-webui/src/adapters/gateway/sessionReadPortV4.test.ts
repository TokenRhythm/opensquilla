import { describe, expect, it, vi } from 'vitest'
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
  SessionReadSessionMissingError,
} from '@/modules/sessionReadLifecycle'
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
        opaque_payload: { snake_value: true },
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
      router_decision: { selected_tier: 'c1' },
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
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('v4 SessionReadPort Adapter', () => {
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
        projectWorkspace: { display_name: 'Workspace One' },
      },
      snapshot: {
        sessionKey: 'alpha',
        events: [{
          semanticKind: 'text-delta',
          payload: { task_id: 'task-snapshot', text_delta: 'hello' },
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
      opaque_payload: { snake_value: true },
    })
    expect(Object.isFrozen(snapshotPayload)).toBe(true)
    expect(Object.isFrozen(snapshotPayload?.opaque_payload)).toBe(true)
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
    await expect(lease.metadata).resolves.toMatchObject({ hydrationComplete: true })
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
      currentPlan: { plan_id: 'plan-1' },
      activePlanRun: { run_id: 'run-1' },
      goal: { goal_id: 'goal-1' },
      tasks: [{ task_id: 'task-1' }],
      activeTask: { task_id: 'task-1' },
      lastTask: { task_id: 'task-0' },
      queuedTaskIds: ['task-2'],
      epoch: 3,
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
        routerDecision: { selected_tier: 'c1' },
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
    await expect(firstRetry).resolves.toMatchObject({ routing: { mode: 'manual' } })
    await expect(secondRetry).resolves.toMatchObject({ routing: { mode: 'manual' } })
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
})
