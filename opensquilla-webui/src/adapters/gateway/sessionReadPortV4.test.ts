import { RpcTimeoutError } from '@/lib/rpc'
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
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('v4 SessionReadPort Adapter', () => {
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
      expect(request?.options).toMatchObject({ timeoutMs: 3000, timeoutAction: 'reject', abortAction: 'reject', expectedGeneration: 7 })
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
