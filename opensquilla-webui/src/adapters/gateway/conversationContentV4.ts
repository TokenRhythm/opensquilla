import type { ConversationCronResult, ConversationEventData, ConversationEventContext, ConversationRoutingSnapshot, ConversationUsage } from '@/modules/conversationEventContent'
import type { ConversationEventProjection, ConversationSemanticEventKind } from '@/modules/conversationEvents'
import { normalizeToolName, normalizeToolPresentation, toolResultIsError } from '@/utils/chat/toolDisplay'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { usageAccountingErrorCode } from '@/utils/chat/usageAccountingFailure'
import type { DecodedConversationEvent } from './conversationEventsV4'
import { ConversationEventContractError, conversationSemanticEventKind, decodeConversationEvent } from './conversationEventsV4'

const STRING_FIELDS = `reason status run_status terminal_message terminal_reason message code group_id
  to_state error_class text reasoning_content model activity_id phase parent_session_key child_session_key
  message_id session_id client_message_id user_message_id surface_id approval_id name input_delta model_call_id
  block_id content_kind target_turn_id client_request_id promoted_from_turn_id promoted_turn_id failure_code error_code
  recovery tier routed_tier routed_model baseline_model decision_id rollout_phase accepted_routing_mode
  source proposer_label proposer_model proposer_provider detail skip_reason
  compaction_id stage durability intent kind sha256 mime created_at store download_url thumbnail_url
  input_mode run_kind coverage_status authoritative_text_snapshot authoritative_reasoning_snapshot replay_gap_reason`.split(/\s+/)
const NUMBER_FIELDS = `epoch stream_seq generation_epoch started_at emitted_at input_tokens output_tokens
  cached_tokens cache_write cost_usd unknown_usage_events old_generation_epoch new_generation_epoch sequence
  retry_attempt retry_limit retry_after_ms finished_at iteration block_index ended_at applied_iteration
  revision proposer_index sample_index elapsed_ms heartbeat_at confidence current_stream_seq`.split(/\s+/)
const BOOLEAN_FIELDS = `preserve_completed_tools terminal heartbeat synthetic_from_text is_error retryable
  fallback_safe fallback routing_applied compacted refused safe_to_send applied user_visible usage_unknown replay_complete`.split(/\s+/)
const CONTENT_FIELDS = ['input', 'arguments', 'result', 'content', 'output', 'error', 'decision', 'router_tier_snapshot'] as const

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function camel(key: string): string {
  return key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase())
}

function alias(source: Record<string, unknown>, key: string, ...alternatives: string[]): unknown {
  for (const name of [key, camel(key), ...alternatives]) {
    const value = source[name]
    if (value !== undefined && value !== null) return value
  }
  return source[key] ?? source[camel(key)]
}

function eventTaskIdentity(source: Record<string, unknown>): string | undefined {
  const direct = source.task_id ?? source.taskId
  if (typeof direct === 'string') return direct
  for (const key of ['active_task', 'activeTask', 'last_task', 'lastTask']) {
    const nested = object(source[key])
    const value = nested.task_id ?? nested.taskId
    if (typeof value === 'string') return value
  }
  return undefined
}

function task(value: unknown): ConversationEventData['active_task'] {
  if (value === null) return null
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const source = object(value)
  const result: Record<string, unknown> = {}
  for (const key of ['task_id', 'turn_id', 'status', 'started_at', 'finished_at', 'terminal_reason', 'task_group_count', 'cancel_requested', 'steer_capability', 'turn_outcome', 'document_mutation_outcome']) {
    const candidate = alias(source, key)
    if (candidate !== undefined) result[key] = candidate
  }
  result.ownershipTaskId = String(source.task_id || source.taskId || source.turn_id || source.turnId || '').trim()
  if ('cancel_requested' in source || 'cancelRequested' in source) {
    result.cancel_requested = source.cancel_requested === true || source.cancelRequested === true
  }
  return result
}

function modelCallSegments(value: unknown): ConversationEventData['model_call_segments'] {
  if (!Array.isArray(value)) return undefined
  return value.map(item => {
    const source = object(item)
    const result: NonNullable<ConversationEventData['model_call_segments']>[number] = {}
    const id = alias(source, 'model_call_id')
    if (typeof id === 'string') result.model_call_id = id
    for (const key of ['iteration', 'start_codepoint', 'end_codepoint'] as const) {
      const raw = alias(source, key)
      const number = Number(raw)
      if (raw != null && Number.isFinite(number)) result[key] = number
    }
    return result
  })
}

function terminalUsage(source: Record<string, unknown>): ConversationUsage {
  const nested = object(source.usage)
  const raw = { ...(source.usage ? nested : source) }
  // Preserve the established merge before collapsing spellings: an outer
  // canonical field can outrank a nested camel-only field, but never replaces
  // an already-present nested field of the same spelling.
  for (const key of ['model_usage_breakdown', 'modelUsageBreakdown', 'ensemble_trace', 'ensembleTrace', 'coverage_status', 'coverageStatus', 'usage_unknown', 'usageUnknown', 'unknown_usage_events', 'unknownUsageEvents']) {
    if (source[key] != null && raw[key] == null) raw[key] = source[key]
  }
  const result: Record<string, unknown> = {}
  for (const key of ['model', 'routed_model', 'routed_tier', 'routing_source', 'coverage_status', 'router_model_call_id', 'decision_id']) {
    const value = alias(raw, key)
    if (typeof value === 'string') result[key] = value
  }
  for (const key of ['input_tokens', 'output_tokens', 'cached_tokens', 'cache_write', 'cache_write_tokens', 'reasoning_tokens', 'cost_usd', 'billed_cost', 'total_tokens', 'estimated_cost_component_usd', 'total_savings_pct', 'total_savings_usd', 'savings_usd', 'savings_pct', 'router_iteration', 'unknown_usage_events']) {
    const value = alias(raw, key)
    if (value != null && Number.isFinite(Number(value))) result[key] = Number(value)
  }
  for (const key of ['usage_unknown', '__savings_ui_suppressed']) {
    const value = alias(raw, key)
    if (value != null) result[key] = Boolean(value)
  }
  for (const key of ['model_usage_breakdown', 'ensemble_trace']) {
    const values = [raw[key], raw[camel(key)]]
    const value = values.find(item => key === 'model_usage_breakdown'
      ? Array.isArray(item) : item && typeof item === 'object' && !Array.isArray(item))
    if (value !== undefined) result[key] = value
  }
  // The outer persisted route plan overrides the smaller nested usage receipt.
  const route = alias(source, 'route_plan') ?? alias(raw, 'route_plan')
  if (route && typeof route === 'object' && !Array.isArray(route)) result.route_plan = route
  const segments = modelCallSegments(alias(raw, 'model_call_segments') ?? alias(source, 'model_call_segments'))
  if (segments) result.model_call_segments = segments
  return result
}

function terminalText(source: Record<string, unknown>): string | null {
  const nested = object(source.usage)
  for (const item of [source, nested]) {
    for (const key of ['text_snapshot', 'textSnapshot']) {
      if (typeof item[key] === 'string') return item[key]
    }
  }
  for (const item of [nested, source]) {
    if (typeof item.text === 'string' && item.text) return item.text
  }
  return null
}

/** Shared live/history routing facts without a session envelope or wire aliases. */
export function projectConversationRoutingSnapshot(value: unknown): ConversationRoutingSnapshot {
  const source = object(value)
  const result: ConversationRoutingSnapshot = {}
  for (const key of ['tier', 'model', 'routed_tier', 'routed_model', 'decision_id', 'rollout_phase'] as const) {
    const value = alias(source, key)
    if (typeof value === 'string') result[key] = value
  }
  for (const [key, value] of [
    ['baseline_model', source.baseline_model || source.baselineModel],
    ['accepted_routing_mode', source.accepted_routing_mode || source.acceptedRoutingMode],
    ['source', source.source || source.routing_source],
  ] as const) {
    if (value != null) result[key] = String(value || '')
  }
  const confidence = source.confidence
  if (typeof confidence === 'number' && Number.isFinite(confidence)) result.confidence = confidence
  for (const key of ['fallback', 'routing_applied'] as const) {
    const value = alias(source, key)
    if (typeof value === 'boolean') result[key] = value
  }
  if (source.decision !== undefined) result.decision = source.decision
  const snapshot = source.router_tier_snapshot ?? source.routerTierSnapshot
  if (snapshot !== undefined) result.router_tier_snapshot = snapshot
  return result
}

/** Finite projection: never spread the incoming payload or rewrite open tool
 * arguments, tool results, user text or other producer-owned content. */
export function projectConversationContent(payload: unknown, kind?: ConversationSemanticEventKind): ConversationEventData {
  const source = object(payload)
  const result: Record<string, unknown> = {}
  for (const key of STRING_FIELDS) {
    const value = alias(source, key)
    if (typeof value === 'string') result[key] = value
  }
  for (const key of NUMBER_FIELDS) {
    const value = alias(source, key)
    if (typeof value === 'number' && Number.isFinite(value)) result[key] = value
  }
  // These display fields historically use truthy fallback, not nullish fallback.
  for (const key of ['compaction_id', 'model_call_id', 'turn_id', 'to_state', 'run_status', 'message_id']) {
    const value = source[key] || source[camel(key)]
    delete result[key]
    if (value != null || key in source || camel(key) in source) result[key] = String(value || '')
  }
  // Clock coercion belongs to activity presentation. Tool/reasoning clocks only
  // trust the producer's numeric canonical timestamp and must not be backfilled.
  for (const key of ['started_at', 'emitted_at']) {
    delete result[key]
    if (typeof source[key] === 'number' && Number.isFinite(source[key])) result[key] = source[key]
  }
  const activityClock = Number(source.started_at ?? source.startedAt ?? source.emitted_at ?? source.emittedAt)
  if (Number.isFinite(activityClock) && activityClock > 0) result.activityStartedAt = activityClock
  for (const key of BOOLEAN_FIELDS) {
    const value = alias(source, key)
    if (typeof value === 'boolean') result[key] = value
  }
  for (const key of ['preserve_completed_tools', 'user_visible']) {
    const value = alias(source, key)
    if (value != null) result[key] = value !== false
  }
  for (const key of CONTENT_FIELDS) {
    const value = alias(source, key)
    if (value !== undefined) result[key] = value
  }
  for (const key of ['key', 'task_id', 'stream_generation', 'assistant_message_id']) {
    const value = key === 'key' ? alias(source, 'key', 'session_key', 'sessionKey') : alias(source, key)
    if (typeof value === 'string') result[key] = value
  }
  const replayComplete = [source.replay_complete, source.replayComplete].find(value => typeof value === 'boolean')
  if (typeof replayComplete === 'boolean') result.replay_complete = replayComplete
  // Preserve the cursor reader's first correctly typed spelling, including
  // false/zero and identifiers containing whitespace. Do not tighten replay.
  for (const [key, names] of [
    ['key', ['key', 'session_key', 'sessionKey']],
    ['stream_generation', ['stream_generation', 'streamGeneration']],
    ['replay_gap_reason', ['replay_gap_reason', 'replayGapReason']],
  ] as const) {
    delete result[key]
    const value = names.map(name => source[name]).find(item => typeof item === 'string' && item)
    if (typeof value === 'string') result[key] = value
  }
  for (const [key, names] of [
    ['epoch', ['epoch']], ['stream_seq', ['stream_seq', 'streamSeq']],
    ['current_stream_seq', ['current_stream_seq', 'currentStreamSeq']],
  ] as const) {
    delete result[key]
    const value = names.map(name => source[name]).find(item => typeof item === 'number' && Number.isFinite(item))
    if (typeof value === 'number') result[key] = value
  }
  for (const key of ['text_snapshot', 'terminal_text_snapshot', 'authoritative_text_snapshot', 'authoritative_reasoning_snapshot', 'block_id']) {
    delete result[key]
    const value = [source[key], source[camel(key)]].find(item => typeof item === 'string')
    if (typeof value === 'string') result[key] = value
    else if (source[key] === null || source[camel(key)] === null) result[key] = null
  }
  const text = alias(source, 'text', 'text_delta')
  if (typeof text === 'string') result.text = text
  if (kind?.startsWith('tool-')) {
    const toolId = source.tool_use_id || source.toolUseId || source.id
    if (typeof toolId === 'string') result.id = toolId
    result.watchdogToolId = String(source.tool_use_id ?? source.toolUseId ?? source.id ?? '')
    result.name = normalizeToolName(source)
    const delta = source.json_fragment ?? source.jsonFragment ?? source.fragment ?? ''
    if (kind === 'tool-use-delta') result.input_delta = typeof delta === 'string' ? delta : String(delta || '')
  } else if (typeof source.id === 'string') result.id = source.id
  if ('approval_id' in source || 'approvalId' in source) {
    result.approval_id = String(source.approval_id ?? source.approvalId ?? '')
  }
  const toolPresentation = normalizeToolPresentation(source)
  if (toolPresentation) result.tool_presentation = toolPresentation
  for (const key of ['active_task', 'last_task', 'changed_task']) {
    const value = task(source[key] || source[camel(key)])
    if (value !== undefined || key in source || camel(key) in source) result[key] = value
  }
  const taskId = eventTaskIdentity(source)
  if (taskId !== undefined) result.task_id = taskId
  for (const key of ['steer_capability', 'turn_outcome', 'document_mutation_outcome', 'execution_status', 'usage', 'route_plan', 'ensemble_trace']) {
    const value = alias(source, key)
    if (value && typeof value === 'object' && !Array.isArray(value)) result[key] = value
  }
  for (const key of ['model_call_segments', 'model_usage_breakdown']) {
    const value = alias(source, key)
    if (Array.isArray(value)) result[key] = value
  }
  for (const [key, allowed] of [
    ['presentation', ['intermediate', 'answer']], ['delivery', ['visible', 'suppressed']],
    ['suppression_reason', ['no_reply', 'heartbeat_ack']],
    ['event_type', ['proposer_start', 'proposer_finish', 'aggregator_start', 'aggregator_finish']],
    ['disposition', ['steering', 'applied', 'rejected', 'promoted', 'cancelled']],
  ] as const) {
    const value = alias(source, key)
    if (typeof value === 'string' && (allowed as readonly string[]).includes(value)) result[key] = value
  }
  if (typeof source.size === 'string' || typeof source.size === 'number') result.size = source.size
  if (source.type === 'subagent_completion') result.type = source.type
  if (kind === 'tool-result') {
    result.result = source.result || source.content || source.output || ''
    result.approvalResult = source.result
    result.is_error = toolResultIsError(source)
  }
  if (kind === 'ensemble-progress') {
    const role = String(source.event_type || '').startsWith('aggregator_') ? 'aggregator' : 'proposer'
    result.watchdogMemberId = [
      role,
      ...['proposer_index', 'sample_index', 'proposer_label', 'proposer_provider', 'proposer_model']
        .map(key => String(source[key] ?? '')),
    ].join(':')
    for (const key of ['proposer_index', 'sample_index', 'input_tokens', 'output_tokens', 'cost_usd', 'elapsed_ms']) {
      if (source[key] != null && Number.isFinite(Number(source[key]))) result[key] = Number(source[key])
    }
  }
  if (kind === 'router-decision') Object.assign(result, projectConversationRoutingSnapshot(source))
  if (kind === 'input-disposition' && source.revision != null && Number.isFinite(Number(source.revision))) {
    result.revision = Number(source.revision)
  }
  if (kind === 'warning') {
    result.warningVisible = !['provider_reasoning_only_retry', 'provider_request_message_limit_recovery_success', 'context_auto_compaction_start', 'context_auto_compaction_retry'].includes(String(source.code || ''))
  }
  if (kind === 'turn-completed') {
    result.finalText = terminalText(source)
    result.usage = terminalUsage(source)
    const segments = modelCallSegments(alias(object(source.usage), 'model_call_segments') ?? alias(source, 'model_call_segments'))
    if (segments) result.model_call_segments = segments
    for (const key of ['input_mode', 'run_kind']) {
      const value = [source[key], source[camel(key)]].find(item => typeof item === 'string' && item.trim())
      if (typeof value === 'string') result[key] = value.trim()
    }
    const turnId = [source.turn_id, source.turnId, source.task_id, source.taskId].find(item => typeof item === 'string' && item.trim())
    if (typeof turnId === 'string') result.completedTurnId = turnId.trim()
  }
  if (kind === 'turn-failed' || kind === 'task-failed' || kind === 'task-timed-out' || kind === 'task-abandoned') {
    result.terminalOutcome = normalizeTurnOutcome({ ...source, turn_id: taskId, status: 'failed' })
    const errorCode = usageAccountingErrorCode(source)
    if (errorCode) result.error_class = errorCode
  }
  return result
}

export function projectConversationContext(meta: unknown): ConversationEventContext {
  const source = object(meta)
  return {
    ...(typeof source.replayed === 'boolean' ? { replayed: source.replayed } : {}),
    ...(typeof source.authoritativeLive === 'boolean' ? { authoritativeLive: source.authoritativeLive } : {}),
  }
}

function cronResult(payload: unknown): ConversationCronResult {
  const source = object(payload)
  const {
    key, epoch, task_id, turn_id, stream_generation, stream_seq, current_stream_seq,
    replay_complete, replay_gap_reason, generation_epoch, assistant_message_id,
    started_at, emitted_at, activityStartedAt,
  } = projectConversationContent(payload)
  const message = object(source.message)
  const projected: NonNullable<ConversationCronResult['message']> = {}
  for (const key of ['role', 'text', 'messageId', 'provenanceKind', 'provenanceSourceTool', 'provenanceSourceSessionKey'] as const) {
    const value = key === 'messageId' ? message.messageId || message.message_id : message[key]
    if (typeof value === 'string') projected[key] = value
  }
  if (typeof message.timestamp === 'string' || typeof message.timestamp === 'number' || message.timestamp === null) projected.timestamp = message.timestamp
  return {
    key, epoch, task_id, turn_id, stream_generation, stream_seq, current_stream_seq,
    replay_complete, replay_gap_reason, generation_epoch, assistant_message_id,
    started_at, emitted_at, activityStartedAt, message: projected,
  }
}

type ConversationContentProjection<Event = ConversationEventProjection> = Event extends {
  kind: 'known'; semanticKind: ConversationSemanticEventKind; payload: unknown
} ? Pick<Event, 'kind' | 'semanticKind' | 'payload'> : never

function projectKnownConversationContent(
  semanticKind: Exclude<ConversationSemanticEventKind, 'unknown'>,
  rawPayload: unknown,
): ConversationContentProjection {
  if (semanticKind === 'turn-committed') {
    const raw = object(rawPayload)
    const optionalText = ['session_id', 'client_message_id', 'user_message_id', 'surface_id', 'stream_generation']
    const optionalSequence = ['stream_seq', 'emitted_at']
    if (raw.schema_version !== 1
      || !['session_key', 'task_id', 'turn_id'].every(key => typeof raw[key] === 'string' && raw[key].trim())
      || raw.status !== 'succeeded' || raw.terminal_reason !== 'completed'
      || typeof raw.finished_at !== 'number' || !Number.isInteger(raw.finished_at) || raw.finished_at < 0
      || !optionalText.every(key => raw[key] === undefined || typeof raw[key] === 'string')
      || !optionalSequence.every(key => raw[key] === undefined || (typeof raw[key] === 'number' && Number.isInteger(raw[key]) && raw[key] >= 0))) {
      throw new ConversationEventContractError('Invalid durable turn receipt')
    }
  }
  if (semanticKind === 'cron-result') {
    return { kind: 'known', semanticKind, payload: cronResult(rawPayload) }
  }
  if (semanticKind === 'provider-activity') {
    const { phase, reason, ...data } = projectConversationContent(rawPayload, semanticKind)
    const phases = ['requesting', 'reasoning', 'retry_wait', 'retrying', 'fallback']
    const reasons = ['initial', 'rate_limited', 'provider_overloaded', 'transport_transient', 'reasoning_only', 'empty_response', 'stream_incomplete', 'invalid_response', 'context_overflow', 'unknown']
    return { kind: 'known', semanticKind, payload: {
      ...data,
      ...(phase && phases.includes(phase) ? { phase } : {}),
      ...(reason && reasons.includes(reason) ? { reason } : {}),
    } }
  }
  if (semanticKind === 'ensemble-progress') {
    const { error, ...data } = projectConversationContent(rawPayload, semanticKind)
    const errorCode = data.error_code?.trim() || (
      typeof error === 'string' && /^proposer cancelled after \d+(?:\.\d+)?s ensemble quorum grace$/.test(error.trim())
        ? 'quorum_cancelled' : ''
    )
    return { kind: 'known', semanticKind, payload: {
      ...data, ...(typeof error === 'string' ? { error } : {}), ...(errorCode ? { error_code: errorCode } : {}),
    } }
  }
  if (semanticKind === 'answer-generation-reset') {
    const { kind: _kind, ...data } = projectConversationContent(rawPayload, semanticKind)
    return { kind: 'known', semanticKind, payload: data }
  }
  if (semanticKind === 'subagent-completed') {
    const { result, ...data } = projectConversationContent(rawPayload, semanticKind)
    return { kind: 'known', semanticKind, payload: { ...data, result: object(result) } }
  }
  const payload = projectConversationContent(rawPayload, semanticKind)
  switch (semanticKind) {
    case 'text-delta': return { kind: 'known', semanticKind, payload }
    case 'tool-use-started': case 'tool-use-delta': case 'tool-use-ended': case 'tool-result':
      return { kind: 'known', semanticKind, payload }
    case 'thinking-started': case 'thinking-delta': case 'thinking-ended':
      return { kind: 'known', semanticKind, payload }
    case 'compaction-progress': return { kind: 'known', semanticKind, payload }
    case 'turn-completed': return { kind: 'known', semanticKind, payload }
    case 'turn-committed': return { kind: 'known', semanticKind, payload }
    case 'input-disposition': return { kind: 'known', semanticKind, payload }
    case 'router-decision': return { kind: 'known', semanticKind, payload }
    case 'warning': return { kind: 'known', semanticKind, payload }
    case 'artifact-created': return { kind: 'known', semanticKind, payload }
    case 'task-abandoned': case 'task-cancelled': case 'task-failed': case 'task-group-completed':
    case 'task-group-failed': case 'task-group-synthesizing': case 'task-group-waiting':
    case 'task-queued': case 'task-running': case 'task-succeeded': case 'task-timed-out':
    case 'turn-failed': case 'state-changed': case 'run-heartbeat': case 'session-epoch-changed':
      return { kind: 'known', semanticKind, payload }
    default: return { kind: 'known', semanticKind, payload }
  }
}

export function projectConversationEvent(decoded: DecodedConversationEvent): ConversationEventProjection {
  const { sessionKey, taskId, turnId, streamGeneration, streamSeq, connectionSeq, generationEpoch } = decoded
  const common = { sessionKey, taskId, turnId, streamGeneration, streamSeq, connectionSeq, generationEpoch, meta: projectConversationContext(decoded.meta) }
  if (!decoded.isKnown || decoded.semanticKind === 'unknown') {
    return { ...common, kind: 'unknown', semanticKind: 'unknown', diagnostic: { eventName: decoded.name } }
  }
  return { ...common, ...projectKnownConversationContent(decoded.semanticKind, decoded.payload) }
}

export function projectConversationSnapshotEvent(eventName: string, payload: unknown) {
  const semanticKind = conversationSemanticEventKind(eventName)
  if (semanticKind === 'unknown') return null
  // Snapshot payloads have their own Contract. Preserve its legacy cursor
  // values for the shared runtime; durable receipts still require strict proof.
  const event = semanticKind === 'turn-committed'
    ? projectConversationEvent(decodeConversationEvent(eventName, payload))
    : projectKnownConversationContent(semanticKind, payload)
  if (event.kind === 'unknown') return null
  if (event.semanticKind === 'cron-result') {
    // Cron rows are not replayed by the live-stream reducer. Keep their identity
    // projection without treating a durable message as a stream error string.
    const { message: _message, ...identity } = event.payload
    return { semanticKind: event.semanticKind, payload: identity }
  }
  return { semanticKind: event.semanticKind, payload: event.payload }
}
