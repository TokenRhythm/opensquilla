import { describe, expect, it, vi } from 'vitest'
import type { TransportEventHandler } from './transportTypes'
import { createConversationEventTransport } from './conversationEventTransport'
import { projectConversationContent } from './conversationContentV4'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import { conversationCursorSignal } from '@/utils/chat/streamEvents'
import chatTypesSource from '@/types/chat.ts?raw'

type ListenerMap = Map<string, Set<TransportEventHandler>>

function harness() {
  const listeners: ListenerMap = new Map()
  const rpc = {
    subscribe(event: string, handler: TransportEventHandler) {
      const bucket = listeners.get(event) ?? new Set<TransportEventHandler>()
      bucket.add(handler)
      listeners.set(event, bucket)
      return { close: () => { bucket.delete(handler) } }
    },
    emit(event: string, ...args: unknown[]) {
      for (const handler of listeners.get(event) ?? []) handler(...args)
    },
    registered(event: string) {
      return listeners.get(event)?.size ?? 0
    },
  }
  return { rpc, transport: createConversationEventTransport(rpc) }
}

describe('conversation event transport adapter', () => {
  it('preserves Cron replay reset facts through the existing shared runtime', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'cron_result', {
      key: 'alpha', epoch: 1, stream_generation: 'g1', current_stream_seq: 0,
      replay_complete: false, replay_gap_reason: 'stream_generation_changed',
      started_at: 1_000, assistant_message_id: 'message-A', message: { text: 'result' },
    })
    const payload = observed.mock.calls[0]?.[0].event.payload
    expect(payload).toMatchObject({
      key: 'alpha', epoch: 1, stream_generation: 'g1', current_stream_seq: 0,
      replay_complete: false, replay_gap_reason: 'stream_generation_changed',
      activityStartedAt: 1_000, assistant_message_id: 'message-A',
    })
    const runtime = createConversationRuntime()
    const cursor = runtime.createCursor('alpha', { sessionEpoch: 1, streamGeneration: null, streamSeq: 9 })
    expect(runtime.observeGeneration(cursor, conversationCursorSignal(payload))).toEqual({
      changed: true, reset: true,
      cursor: { sessionKey: 'alpha', sessionEpoch: 1, streamGeneration: 'g1', streamSeq: 0 },
    })
  })

  it.each([
    { wire: { task_id: '', taskId: 'task-A' }, canonical: { task_id: '' }, ownershipTaskId: 'task-A' },
    { wire: { task_id: 0, taskId: 'task-A' }, canonical: { task_id: 0 }, ownershipTaskId: 'task-A' },
    { wire: { task_id: false, taskId: 'task-A' }, canonical: { task_id: false }, ownershipTaskId: 'task-A' },
    { wire: { task_id: ' ', taskId: 'task-A' }, canonical: { task_id: ' ' }, ownershipTaskId: '' },
    { wire: { task_id: ' task-A ', taskId: 'task-B' }, canonical: { task_id: ' task-A ' }, ownershipTaskId: 'task-A' },
    { wire: { turn_id: '', turnId: 'task-A' }, canonical: { turn_id: '' }, ownershipTaskId: 'task-A' },
    { wire: { turn_id: ' ', turnId: 'task-A' }, canonical: { turn_id: ' ' }, ownershipTaskId: '' },
  ])('keeps task ownership separate from outcome identity authority: %j', ({ wire, canonical, ownershipTaskId }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'sessions.changed', {
      key: 'alpha', reason: 'task_terminal', active_task: { task_id: 'task-B' },
      last_task: { ...wire, status: 'cancelled' },
    })
    const event = observed.mock.calls[0]?.[0]
    expect(event.kind).toBe('sessions-changed')
    expect(event.payload.task_id).toBe('task-B')
    expect(event.payload.last_task).toMatchObject({ ...canonical, ownershipTaskId, status: 'cancelled' })
  })

  it.each([
    { event: 'answer_generation_reset', wire: { old_generation_epoch: 1, new_generation_epoch: 2, authoritative_text_snapshot: '', authoritativeTextSnapshot: 'stale', authoritative_reasoning_snapshot: '', authoritativeReasoningSnapshot: 'stale' }, expected: { authoritative_text_snapshot: '', authoritative_reasoning_snapshot: '' } },
    { event: 'answer_generation_reset', wire: { old_generation_epoch: '', oldGenerationEpoch: 1, new_generation_epoch: 2 }, expected: { old_generation_epoch: undefined } },
    { event: 'answer_generation_reset', wire: { preserve_completed_tools: '', preserveCompletedTools: false }, expected: { preserve_completed_tools: true } },
    { event: 'thinking_start', wire: { block_id: '', blockId: 'stale', block_index: '', blockIndex: 9, content_kind: '', contentKind: 'summary' }, expected: { block_id: '', block_index: undefined, content_kind: '' } },
    { event: 'thinking_start', wire: { block_id: false, blockId: 'legacy' }, expected: { block_id: 'legacy' } },
    { event: 'thinking_end', wire: { ended_at: '', endedAt: 10 }, expected: { ended_at: undefined } },
    { event: 'text_delta', wire: { assistant_message_id: '', assistantMessageId: 'other', text: 'safe' }, expected: { assistant_message_id: '' } },
    { event: 'text_delta', wire: { replay_complete: 0, replayComplete: true, text: 'safe' }, expected: { replay_complete: true } },
    { event: 'state_change', wire: { to_state: false, toState: 'running' }, expected: { to_state: 'running' } },
    { event: 'compaction', wire: { compaction_id: 0, compactionId: 'compact', user_visible: '', userVisible: false }, expected: { compaction_id: 'compact', user_visible: true } },
    { event: 'tool_use_delta', wire: { json_fragment: 'old', input_delta: 'new' }, expected: { input_delta: 'old' } },
    { event: 'tool_use_delta', wire: { json_fragment: '', fragment: 'suffix' }, expected: { input_delta: '' } },
    { event: 'tool_use_delta', wire: { json_fragment: 7 }, expected: { input_delta: '7' } },
    { event: 'tool_use_start', wire: { name: '', tool_name: 'shell', tool_use_id: 't' }, expected: { name: '' } },
    { event: 'tool_use_start', wire: { function: { name: 'shell' }, tool_use_id: 't' }, expected: { name: 'shell' } },
    { event: 'tool_use_start', wire: { tool_use_id: '', toolUseId: 't', name: 'shell' }, expected: { id: 't', watchdogToolId: '' } },
    { event: 'cron_result', wire: { message: { text: 'result', messageId: 'canonical', message_id: 'legacy' } }, expected: { message: { text: 'result', messageId: 'canonical' } } },
    { event: 'provider_activity', wire: { started_at: '1000', phase: 'requesting' }, expected: { activityStartedAt: 1000, started_at: undefined } },
    { event: 'provider_activity', wire: { started_at: '', startedAt: 1000, emitted_at: 2000, phase: 'requesting' }, expected: { activityStartedAt: undefined, started_at: undefined, emitted_at: 2000 } },
    { event: 'tool_use_start', wire: { name: 'inspect', tool_use_id: 'tool-1', started_at: '1000' }, expected: { activityStartedAt: 1000, started_at: undefined } },
    { event: 'ensemble_progress', wire: { event_type: 'proposer_finish', proposer_model: 'model-a', input_tokens: '12', cost_usd: '0.25', proposer_index: '2' }, expected: { input_tokens: 12, cost_usd: 0.25, proposer_index: 2, watchdogMemberId: 'proposer:2::::model-a' } },
    { event: 'ensemble_progress', wire: { event_type: 'proposer_start', proposer_index: '', proposerIndex: 1 }, expected: { proposer_index: 0, watchdogMemberId: 'proposer:::::' } },
    { event: 'input_disposition', wire: { revision: '2' }, expected: { revision: 2 } },
    { event: 'done', wire: { model_call_segments: [{ model_call_id: 'call-1', iteration: '1', start_codepoint: '', startCodepoint: 1, end_codepoint: '4' }] }, expected: { model_call_segments: [{ model_call_id: 'call-1', iteration: 1, start_codepoint: 0, end_codepoint: 4 }] } },
    { event: 'done', wire: { usage: { input_tokens: '12', cost_usd: '0.25' } }, expected: { usage: { input_tokens: 12, cost_usd: 0.25 } } },
    { event: 'done', wire: { usage: { input_tokens: '', inputTokens: 12, cost_usd: '', costUsd: 0.25, coverage_status: '', coverageStatus: 'partial', usage_unknown: '', usageUnknown: true } }, expected: { usage: { input_tokens: 0, cost_usd: 0, coverage_status: '', usage_unknown: false } } },
    { event: 'done', wire: { usage: { model_usage_breakdown: false, modelUsageBreakdown: [{ model: 'model-a', cost_usd: 1 }] } }, expected: { usage: { model_usage_breakdown: [{ model: 'model-a', cost_usd: 1 }] } } },
    { event: 'router_decision', wire: { accepted_routing_mode: '', acceptedRoutingMode: 'ensemble', source: '', routing_source: 'ensemble', baseline_model: '', baselineModel: 'baseline' }, expected: { accepted_routing_mode: 'ensemble', source: 'ensemble', baseline_model: 'baseline' } },
    ...['provider_reasoning_only_retry', 'provider_request_message_limit_recovery_success', 'context_auto_compaction_start', 'context_auto_compaction_retry'].map(code => ({ event: 'warning', wire: { code }, expected: { warningVisible: false } })),
  ])('preserves the established field selection for $event: $wire', ({ event, wire, expected }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', event, { key: 'alpha', ...wire })
    const projection = observed.mock.calls[0]?.[0]
    expect(projection.kind).toBe('conversation')
    for (const [field, value] of Object.entries(expected)) {
      expect(projection.event.payload[field]).toEqual(value)
    }
  })

  it('does not restore legacy event DTO exports or re-exports', () => {
    for (const name of [
      'StreamEventEnvelope', 'SessionEventPayload', 'AnswerGenerationResetPayload',
      'WarningPayload', 'ArtifactStateEventPayload', 'ProviderActivityPayload',
      'CronResultMessagePayload', 'CronResultPayload', 'SubagentCompletionPayload',
      'TextDeltaPayload', 'SessionDonePayload', 'TurnCommittedPayload',
      'ToolUsePayload', 'ToolDeltaPayload', 'ToolEndPayload', 'ToolResultPayload',
      'InputDispositionPayload', 'RouterDecisionPayload', 'EnsembleProgressPayload',
      'CompactionPayload',
    ]) {
      expect(chatTypesSource).not.toMatch(new RegExp(`\\b${name}\\b`))
    }
  })

  it('keeps render fallback separate from approval authority and task cancellation', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    const content = { kind: 'user_input', paused: true, request_id: 'request-1', clarify_schema: { fields: [{ name: 'answer', type: 'string' }] } }
    rpc.emit('*', 'tool_result', { key: 'alpha', content })
    expect(observed.mock.calls[0]?.[0].event.payload).toMatchObject({ result: content })
    expect(observed.mock.calls[0]?.[0].event.payload.approvalResult).toBeUndefined()
    rpc.emit('*', 'sessions.changed', { key: 'alpha', run_status: false, runStatus: 'failed', active_task: { task_id: 't', cancel_requested: false, cancelRequested: true } })
    expect(observed.mock.calls[1]?.[0].payload).toMatchObject({ run_status: 'failed', active_task: { cancel_requested: true } })
  })

  it.each([
    { text: 'fallback', text_snapshot: '', usage: { text: 'nested' }, expected: '' },
    { text_snapshot: null, textSnapshot: 'outer alias', usage: { text_snapshot: 'nested' }, expected: 'outer alias' },
    { text: 'outer', usage: { textSnapshot: 'nested snapshot' }, expected: 'nested snapshot' },
    { text: 'outer', usage: { text: 'nested' }, expected: 'nested' },
    { text: '', usage: { text: '' }, expected: null },
  ])('resolves terminal text before the consumer with legacy precedence: %j', ({ expected, ...payload }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'session.event.done', { key: 'alpha', ...payload })
    expect(observed.mock.calls[0]?.[0].event.payload.finalText).toBe(expected)
  })

  it('projects terminal usage and provenance without a second consumer normalization', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    const directRoute = { selected: { model: 'chosen' } }
    rpc.emit('*', 'session.event.done', {
      key: 'alpha', turnId: ' turn-1 ', inputMode: ' chat ', runKind: ' foreground ',
      modelUsageBreakdown: [{ model: 'chosen', inputTokens: 7 }],
      modelCallSegments: [{ modelCallId: 'call-1', startCodepoint: 0, endCodepoint: 4 }],
      routePlan: directRoute, coverageStatus: 'complete', usageUnknown: false,
      usage: { inputTokens: 7, outputTokens: 2, costUsd: 0, cache_write: 3, route_plan: { selected: { model: 'smaller' } } },
    })
    const payload = observed.mock.calls[0]?.[0].event.payload
    expect(payload).toMatchObject({ turn_id: ' turn-1 ', completedTurnId: 'turn-1', input_mode: 'chat', run_kind: 'foreground', usage: {
      input_tokens: 7, output_tokens: 2, cost_usd: 0, cache_write: 3,
      coverage_status: 'complete', usage_unknown: false, route_plan: directRoute,
      model_usage_breakdown: [{ model: 'chosen', inputTokens: 7 }],
    }, model_call_segments: [{ model_call_id: 'call-1', start_codepoint: 0, end_codepoint: 4 }] })
    expect(payload.usage).not.toHaveProperty('inputTokens')
  })

  it('preserves outer canonical usage evidence over nested camel-only fields', () => {
    const content = projectConversationContent({
      coverage_status: 'partial', usage_unknown: true, unknown_usage_events: 2,
      model_usage_breakdown: [{ model: 'outer', cost_usd: 2 }],
      usage: { coverageStatus: 'complete', usageUnknown: false, unknownUsageEvents: 0,
        modelUsageBreakdown: [{ model: 'inner', cost_usd: 1 }] },
    }, 'turn-completed')
    expect(content.usage).toMatchObject({ coverage_status: 'partial', usage_unknown: true,
      unknown_usage_events: 2, model_usage_breakdown: [{ model: 'outer', cost_usd: 2 }] })
  })

  it('keeps each established physical usage evidence field', () => {
    const usage = { billed_cost: 0.25, total_tokens: 20, cache_write_tokens: 10, estimated_cost_component_usd: 0.125 }
    expect(projectConversationContent({ usage }, 'turn-completed').usage).toMatchObject(usage)
  })

  it('keeps all cursor replay facts while projecting both accepted spellings', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'text_delta', {
      sessionKey: 'alpha', streamGeneration: 'g1', streamSeq: 4, epoch: 2,
      currentStreamSeq: 8, replayComplete: false, replayGapReason: 'retention', text: 'text',
    })
    expect(observed.mock.calls[0]?.[0].event.payload).toMatchObject({
      key: 'alpha', stream_generation: 'g1', stream_seq: 4, epoch: 2,
      current_stream_seq: 8, replay_complete: false, replay_gap_reason: 'retention',
    })
  })

  it.each([
    { task_id: 'canonical', taskId: 'alias', expected: 'canonical' },
    { task_id: '', taskId: 'alias', active_task: { task_id: 'nested' }, expected: '' },
    { taskId: 'camel', expected: 'camel' },
    { task_id: 7, active_task: {}, activeTask: { taskId: 'nested-camel' }, expected: 'nested-camel' },
    { last_task: { task_id: 'nested-last' }, expected: 'nested-last' },
  ])('keeps legacy task identity precedence in the adapter: %j', ({ expected, ...wire }) => {
    // Directory/snapshot projection also accepts unversioned legacy payloads.
    // Conflicting event envelope aliases remain rejected by the event decoder.
    expect(projectConversationContent({ key: 'alpha', ...wire }, 'turn-failed').task_id).toBe(expected)
  })

  it('retains terminal proof when the accepted task identity uses nested camel fields', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'session.event.error', {
      key: 'alpha', activeTask: { taskId: 'task-A', status: 'failed' },
      code: 'usage_accounting_busy', usage_call_index: 1, no_prior_provider_dispatch: true,
      replay_safe: true, user_message_id: 'user-primary',
      turn_outcome: { kind: 'blocked', reason: 'usage_accounting_busy' },
    })
    expect(observed.mock.calls[0]?.[0].event.payload.terminalOutcome).toMatchObject({
      turnId: 'task-A', replaySafe: true, noPriorProviderDispatch: true, userMessageId: 'user-primary',
    })
  })

  it('exposes typed content and keeps envelope aliases and unrelated fields private', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })

    rpc.emit('*', 'text_delta', {
      sessionKey: 'agent:main:alpha', streamSeq: 3, taskId: 'task-1',
      text: 'hi', modelCallId: 'call-1', unrelated: 'not business content',
    }, { replayed: false, wireOnly: 'private' })

    expect(event.mock.calls[0]?.[0]).toEqual({
      kind: 'conversation',
      event: expect.objectContaining({
        semanticKind: 'text-delta',
        payload: { key: 'agent:main:alpha', stream_seq: 3, task_id: 'task-1', text: 'hi', model_call_id: 'call-1' },
        meta: { replayed: false },
      }),
    })
    expect(event.mock.calls[0]?.[0].event).not.toHaveProperty('rawPayload')
    expect(event.mock.calls[0]?.[0].event).not.toHaveProperty('legacy')
  })

  it('uses one wildcard listener and one connection-state listener', () => {
    const { rpc, transport } = harness()
    const detach = transport.subscribe({})

    expect(rpc.registered('*')).toBe(1)
    expect(rpc.registered('_state')).toBe(1)
    detach()
    expect(rpc.registered('*')).toBe(0)
    expect(rpc.registered('_state')).toBe(0)
  })

  it('decodes aliases into one canonical content projection', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })

    const payload = { sessionKey: 'agent:main:alpha', streamSeq: 3, text: 'hi' }
    rpc.emit('*', 'text_delta', payload, { replayed: false })

    expect(event).toHaveBeenCalledTimes(1)
    expect(event.mock.calls[0]?.[0]).toMatchObject({
      kind: 'conversation',
      event: { kind: 'known', semanticKind: 'text-delta',
        payload: { key: 'agent:main:alpha', stream_seq: 3, text: 'hi' },
        meta: { replayed: false },
      },
    })
  })

  it('keeps directory changes in the same listener without treating them as conversation frames', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { key: 'agent:main:alpha', reason: 'renamed' }

    rpc.emit('*', 'sessions.changed', payload, {})

    expect(event).toHaveBeenCalledWith({
      kind: 'sessions-changed',
      payload,
    })
  })

  it('quarantines malformed frames but does not break the wildcard stream', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    const error = vi.fn()
    transport.subscribe({ onEvent: event, onDecodeError: error })

    rpc.emit('*', 'presence', { value: true }, {})

    expect(event).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'invalid',
    }))
    expect(error).toHaveBeenCalledTimes(1)
  })

  it('projects approval aliases before they reach business consumers', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { approval_id: 'approval-1' }

    rpc.emit('*', 'exec.approval.requested', payload, {})
    rpc.emit('*', 'plugin.approval.resolved', payload, {})

    expect(event.mock.calls.map(call => call[0])).toEqual([
      { kind: 'approval', action: 'requested', sessionKey: null, payload },
      { kind: 'approval', action: 'resolved', sessionKey: null, payload },
    ])
  })

  it('leaves duplicate fencing to ConversationRuntime', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { session_key: 'agent:main:alpha', stream_seq: 7, text: 'same' }

    rpc.emit('*', 'session.event.text_delta', payload, {})
    rpc.emit('*', 'text_delta', payload, {})

    expect(event).toHaveBeenCalledTimes(2)
    expect(event.mock.calls.map(call => call[0].event.semanticKind))
      .toEqual(['text-delta', 'text-delta'])
  })

  it.each([
    { schema_version: 2 }, { task_id: null }, { session_key: '' }, { turn_id: '' },
    { stream_seq: -1 }, { stream_seq: 1.5 }, { emitted_at: -1 },
    { finished_at: 1.5 }, { session_id: 7 }, { client_message_id: null },
    { user_message_id: false }, { surface_id: [] }, { stream_generation: null },
  ])('rejects an invalid durable receipt before it can leave the adapter: %j', (invalid) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'session.event.turn_committed', {
      schema_version: 1, session_key: 'agent:main:alpha', task_id: 'task-1', turn_id: 'turn-1',
      status: 'succeeded', terminal_reason: 'completed', finished_at: 10, ...invalid,
    })
    expect(observed).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'invalid' }))
  })

  it('preserves user-owned tool input and result without exposing additional envelope fields', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    const input = { stream_seq: 'business value', nested: { session_key: 'not an envelope' } }
    const result = { output: ['done'], arbitraryBusinessField: false }
    rpc.emit('*', 'tool_result', { key: 'alpha', tool_use_id: 'tool-1', name: 'inspect', input, result, internalHint: true })
    const event = observed.mock.calls[0]?.[0].event
    expect(event.payload.input).toBe(input)
    expect(event.payload.result).toBe(result)
    expect(event.payload.id).toBe('tool-1')
    expect(event.payload).not.toHaveProperty('internalHint')
    expect(event.payload).not.toHaveProperty('tool_use_id')
  })

  it('keeps malformed tool presentation fail-closed instead of dropping the policy', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'tool_use_start', {
      key: 'alpha', tool_use_id: 'tool-1', name: 'inspect',
      input: { privateArgument: 'synthetic' }, tool_presentation: null,
    })
    expect(observed.mock.calls[0]?.[0].event.payload.tool_presentation).toMatchObject({
      argumentDisplay: 'primary', primaryArguments: [], lifecycleDisplay: 'boundary',
    })
  })

  it('forwards connection state through the same lifecycle owner', () => {
    const { rpc, transport } = harness()
    const state = vi.fn()
    transport.subscribe({ onConnectionState: state })

    rpc.emit('_state', 'connected')

    expect(state).toHaveBeenCalledWith('connected')
  })

  it.each([
    { result: 0, content: 'content fallback', output: 'unused', expected: 'content fallback' },
    { result: false, content: '', output: { message: 'output fallback' }, expected: { message: 'output fallback' } },
    { result: 'canonical result', content: 'unused', expected: 'canonical result' },
  ])('projects tool result aliases with the established fallback precedence: %j', ({ expected, ...payload }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'tool_result', { key: 'alpha', id: 'generic-id', tool_use_id: 'tool-1', toolName: 'inspect', ...payload })
    expect(observed.mock.calls[0]?.[0].event.payload).toMatchObject({ id: 'tool-1', name: 'inspect', result: expected })
  })

  it.each([
    { error: 'proposer cancelled after 1.5s ensemble quorum grace', expected: 'quorum_cancelled' },
    { error: 'proposer cancelled after 1.5s ensemble quorum grace', error_code: 'provider_error', expected: 'provider_error' },
  ])('projects legacy quorum cancellation without overwriting an explicit reason: %j', ({ expected, ...payload }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'session.event.ensemble_progress', { key: 'alpha', event_type: 'proposer_finish', ...payload })
    expect(observed.mock.calls[0]?.[0].event.payload.error_code).toBe(expected)
  })

  it('preserves router presentation facts and nested usage projections', () => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    const routePlan = { selected: { provider: 'test', model: 'model-a' } }
    const usage = { input_tokens: 12, route_plan: routePlan, model_usage_breakdown: [{ model: 'model-a', input_tokens: 12 }] }
    rpc.emit('*', 'session.event.router_decision', {
      key: 'alpha', routed_tier: 'c1', routed_model: 'model-a', baselineModel: 'model-b',
      confidence: 0.8, fallback: false, rollout_phase: 'enabled', acceptedRoutingMode: 'router',
      decision_id: 'decision-1', usage, route_plan: routePlan,
    })
    expect(observed.mock.calls[0]?.[0].event.payload).toMatchObject({
      routed_tier: 'c1', routed_model: 'model-a', baseline_model: 'model-b', confidence: 0.8,
      fallback: false, rollout_phase: 'enabled', accepted_routing_mode: 'router', decision_id: 'decision-1',
    })
    expect(observed.mock.calls[0]?.[0].event.payload.usage).toBe(usage)
    expect(observed.mock.calls[0]?.[0].event.payload.route_plan).toBe(routePlan)
  })

  it.each([
    { is_error: false, isError: true, expected: true },
    { execution_status: { status: 'success' }, is_error: true, expected: false },
    { executionStatus: { status: 'timeout' }, is_error: false, expected: true },
  ])('preserves the established tool failure precedence: %j', ({ expected, ...payload }) => {
    const { rpc, transport } = harness()
    const observed = vi.fn()
    transport.subscribe({ onEvent: observed })
    rpc.emit('*', 'tool_result', { key: 'alpha', tool_use_id: 'tool-1', ...payload })
    expect(observed.mock.calls[0]?.[0].event.payload.is_error).toBe(expected)
  })
})
