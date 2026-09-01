import {
  CONVERSATION_EVENTS_EVENT_METADATA,
  CONVERSATION_EVENTS_SCHEMA_VERSION,
  type V4ConversationEventFrame,
} from '@/contracts/generated/v4/conversationEvents'
import { validateConversationEventFrame } from '@/contracts/generated/v4/conversationEventsValidators.mjs'
import type { ConversationSemanticEventKind } from '@/modules/conversationEvents'

export type { ConversationSemanticEventKind } from '@/modules/conversationEvents'

/**
 * The event family is intentionally open for additive rollout.  Keep the
 * manifest in the language-neutral Contract and derive the runtime set here;
 * a page or composable must never maintain a second list of wire names.
 */
export const CONVERSATION_EVENT_WIRE_NAMES = Object.freeze(
  CONVERSATION_EVENTS_EVENT_METADATA.wireNames.filter(
    value => typeof value === 'string',
  ),
)

const KNOWN_EVENT_NAMES = new Set<string>(CONVERSATION_EVENT_WIRE_NAMES)
const EVENT_PREFIX = 'session.event.'
const BARE_EVENT_ALIASES = new Set(
  CONVERSATION_EVENT_WIRE_NAMES
    .filter(name => name.startsWith(EVENT_PREFIX))
    .map(name => name.slice(EVENT_PREFIX.length)),
)

export type ConversationEventKind = 'known' | 'unknown'

/**
 * Business-facing event vocabulary. Wire aliases and protocol namespaces are
 * deliberately absent: consumers switch on these meanings, never on v4 event
 * names. Additive wire events project to `unknown` until their domain meaning
 * is explicitly adopted.
 */
const SEMANTIC_EVENT_KIND_BY_WIRE_NAME = new Map<string, ConversationSemanticEventKind>([
  ['chat.done', 'turn-completed'],
  ['exec.approval.requested', 'approval-requested'],
  ['exec.approval.resolved', 'approval-resolved'],
  ['plugin.approval.requested', 'approval-requested'],
  ['plugin.approval.resolved', 'approval-resolved'],
  ['session.epoch_changed', 'session-epoch-changed'],
  ['session.event.answer_generation_reset', 'answer-generation-reset'],
  ['session.event.approval_required', 'approval-requested'],
  ['session.event.artifact', 'artifact-created'],
  ['session.event.artifact_state', 'artifact-state-changed'],
  ['session.event.collaboration_mode', 'collaboration-mode-changed'],
  ['session.event.compaction', 'compaction-progress'],
  ['session.event.cron_result', 'cron-result'],
  ['session.event.done', 'turn-completed'],
  ['session.event.ensemble_progress', 'ensemble-progress'],
  ['session.event.error', 'turn-failed'],
  ['session.event.goal', 'goal-changed'],
  ['session.event.goal_run', 'goal-run-changed'],
  ['session.event.input_disposition', 'input-disposition'],
  ['session.event.meta_preflight', 'meta-preflight'],
  ['session.event.meta_run_announced', 'meta-run-announced'],
  ['session.event.meta_run_completed', 'meta-run-completed'],
  ['session.event.meta_step_state', 'meta-step-state'],
  ['session.event.plan_revision', 'plan-revision'],
  ['session.event.plan_run', 'plan-run'],
  ['session.event.provider_activity', 'provider-activity'],
  ['session.event.router_control_replay', 'router-control-replay'],
  ['session.event.router_decision', 'router-decision'],
  ['session.event.run_heartbeat', 'run-heartbeat'],
  ['session.event.state_change', 'state-changed'],
  ['session.event.steer', 'steer-received'],
  ['session.event.subagent_completion', 'subagent-completed'],
  ['session.event.task_group.done', 'task-group-completed'],
  ['session.event.task_group.failed', 'task-group-failed'],
  ['session.event.task_group.synthesizing', 'task-group-synthesizing'],
  ['session.event.task_group.waiting', 'task-group-waiting'],
  ['session.event.text_delta', 'text-delta'],
  ['session.event.thinking', 'thinking-delta'],
  ['session.event.thinking_end', 'thinking-ended'],
  ['session.event.thinking_start', 'thinking-started'],
  ['session.event.tool_result', 'tool-result'],
  ['session.event.tool_use_delta', 'tool-use-delta'],
  ['session.event.tool_use_end', 'tool-use-ended'],
  ['session.event.tool_use_start', 'tool-use-started'],
  ['session.event.turn_committed', 'turn-committed'],
  ['session.event.warning', 'warning'],
  ['task.abandoned', 'task-abandoned'],
  ['task.cancelled', 'task-cancelled'],
  ['task.failed', 'task-failed'],
  ['task.queued', 'task-queued'],
  ['task.running', 'task-running'],
  ['task.succeeded', 'task-succeeded'],
  ['task.timeout', 'task-timed-out'],
])

export type KnownConversationEventName = (typeof CONVERSATION_EVENT_WIRE_NAMES)[number]

interface DecodedConversationEventCommon {
  /** Object payloads are safe to inspect; primitive payloads remain in rawPayload. */
  payload: Readonly<Record<string, unknown>> | null
  rawPayload: unknown
  meta: Readonly<Record<string, unknown>> | null
  sessionKey: string | null
  taskId: string | null
  turnId: string | null
  streamGeneration: string | null
  streamSeq: number | null
  connectionSeq: number | null
  generationEpoch: number | null
  schemaVersion: number | null
  /** True for current v4 events that predate the schema_version field. */
  legacy: boolean
  semanticKind: ConversationSemanticEventKind
}

/** Discriminated union used by the future ConversationRuntime consumer. */
export type KnownConversationEvent = DecodedConversationEventCommon & {
  name: KnownConversationEventName
  kind: 'known'
  readonly isKnown: true
}

export type UnknownConversationEvent = DecodedConversationEventCommon & {
  /** Unknown additive names are retained for diagnostics, never dispatched. */
  name: string
  kind: 'unknown'
  readonly isKnown: false
}

export type DecodedConversationEvent = KnownConversationEvent | UnknownConversationEvent

export class ConversationEventContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ConversationEventContractError'
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : null
}

function textAlias(value: Record<string, unknown>, ...names: string[]): string | null {
  const found: Array<{ name: string, value: string }> = []
  for (const name of names) {
    const candidate = value[name]
    if (candidate === undefined || candidate === null) continue
    if (typeof candidate !== 'string' || !candidate.trim()) {
      throw new ConversationEventContractError(
        `${CONVERSATION_EVENTS_EVENT_METADATA.name} ${name} must be a non-empty string`,
      )
    }
    found.push({ name, value: candidate.trim() })
  }
  const unique = new Set(found.map(item => item.value))
  if (unique.size > 1) {
    throw new ConversationEventContractError(
      `${CONVERSATION_EVENTS_EVENT_METADATA.name} has conflicting aliases: ${found.map(item => item.name).join(', ')}`,
    )
  }
  return unique.values().next().value ?? null
}

function integerAlias(value: Record<string, unknown>, ...names: string[]): number | null {
  const found: Array<{ name: string, value: number }> = []
  for (const name of names) {
    const candidate = value[name]
    if (candidate === undefined || candidate === null) continue
    if (
      typeof candidate !== 'number'
      || !Number.isFinite(candidate)
      || !Number.isInteger(candidate)
      || candidate < 0
    ) {
      throw new ConversationEventContractError(
        `${CONVERSATION_EVENTS_EVENT_METADATA.name} ${name} must be a non-negative JSON integer`,
      )
    }
    found.push({ name, value: candidate })
  }
  const unique = new Set(found.map(item => item.value))
  if (unique.size > 1) {
    throw new ConversationEventContractError(
      `${CONVERSATION_EVENTS_EVENT_METADATA.name} has conflicting numeric aliases: ${found.map(item => item.name).join(', ')}`,
    )
  }
  return unique.values().next().value ?? null
}

/** Return a canonical name for the legacy bare/suffixed spellings. */
export function canonicalConversationEventName(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new ConversationEventContractError(
      `${CONVERSATION_EVENTS_EVENT_METADATA.name} event name must be a non-empty string`,
    )
  }
  const name = value.trim()
  if (BARE_EVENT_ALIASES.has(name)) return `${EVENT_PREFIX}${name}`
  if (name === 'session.answer_generation_reset.v1') {
    return 'session.event.answer_generation_reset'
  }
  if (name === 'session.turn_committed.v1') {
    return 'session.event.turn_committed'
  }
  return name
}

/** Project any canonical or legacy wire spelling into the domain vocabulary. */
export function conversationSemanticEventKind(value: unknown): ConversationSemanticEventKind {
  const name = canonicalConversationEventName(value)
  return SEMANTIC_EVENT_KIND_BY_WIRE_NAME.get(name) ?? 'unknown'
}

function validateFrame(frame: unknown): Record<string, unknown> {
  const value = objectValue(frame)
  if (!value) {
    throw new ConversationEventContractError(
      `${CONVERSATION_EVENTS_EVENT_METADATA.name} frame must be a JSON object`,
    )
  }
  value.event = canonicalConversationEventName(value.event)
  if (!validateConversationEventFrame(value as V4ConversationEventFrame)) {
    throw new ConversationEventContractError(
      `${CONVERSATION_EVENTS_EVENT_METADATA.name} frame violated the v4 Contract`,
    )
  }
  return value
}

function decodeValidatedFrame(frame: unknown): DecodedConversationEvent {
  const originalName = objectValue(frame)?.event
  const value = validateFrame(frame)
  const name = value.event as string
  const legacyName = (
    typeof originalName === 'string'
    && originalName.trim() !== name
  )
  const rawPayload = value.payload
  const payload = objectValue(rawPayload)
  const meta = objectValue(value.meta)
  const connectionSeq = integerAlias(value, 'seq')

  let schemaVersion: number | null = null
  let sessionKey: string | null = null
  let taskId: string | null = null
  let turnId: string | null = null
  let streamGeneration: string | null = null
  let streamSeq: number | null = null
  let generationEpoch: number | null = null
  let legacy = true

  if (payload) {
    schemaVersion = integerAlias(payload, 'schema_version')
    if (
      schemaVersion !== null
      && schemaVersion !== CONVERSATION_EVENTS_SCHEMA_VERSION
    ) {
      throw new ConversationEventContractError(
        `${CONVERSATION_EVENTS_EVENT_METADATA.name} schema_version must be ${CONVERSATION_EVENTS_SCHEMA_VERSION}`,
      )
    }
    sessionKey = textAlias(payload, 'key', 'session_key', 'sessionKey')
    taskId = textAlias(payload, 'task_id', 'taskId')
    turnId = textAlias(payload, 'turn_id', 'turnId')
    streamGeneration = textAlias(payload, 'stream_generation', 'streamGeneration')
    streamSeq = integerAlias(payload, 'stream_seq', 'streamSeq')
    generationEpoch = integerAlias(payload, 'generation_epoch', 'generationEpoch')
    integerAlias(payload, 'emitted_at', 'emittedAt')
    legacy = schemaVersion === null || legacyName
  }

  const isKnown = KNOWN_EVENT_NAMES.has(name)
  const semanticKind = SEMANTIC_EVENT_KIND_BY_WIRE_NAME.get(name) ?? 'unknown'
  const decoded: DecodedConversationEvent = isKnown
    ? {
        name: name as KnownConversationEventName,
        kind: 'known',
        isKnown: true,
        payload,
        rawPayload,
        meta,
        sessionKey,
        taskId,
        turnId,
        streamGeneration,
        streamSeq,
        connectionSeq,
        generationEpoch,
        schemaVersion,
        legacy,
        semanticKind,
      }
    : {
        name,
        kind: 'unknown',
        isKnown: false,
        payload,
        rawPayload,
        meta,
        sessionKey,
        taskId,
        turnId,
        streamGeneration,
        streamSeq,
        connectionSeq,
        generationEpoch,
        schemaVersion,
        legacy,
        semanticKind,
      }
  return decoded
}

/** Decode a complete v4 event frame (snapshot/replay form). */
export function decodeConversationEventFrame(frame: unknown): DecodedConversationEvent {
  return decodeValidatedFrame(frame)
}

/**
 * Decode either a complete frame or the `(event, payload, meta)` values
 * delivered by `RpcClient.on`.  The overload keeps transport details out of
 * the returned projection while preserving the original payload by reference
 * only through `rawPayload` (the inspectable object is a defensive copy).
 */
export function decodeConversationEvent(frame: unknown): DecodedConversationEvent
export function decodeConversationEvent(
  eventName: unknown,
  payload: unknown,
  meta?: unknown,
  connectionSeq?: unknown,
): DecodedConversationEvent
export function decodeConversationEvent(
  eventOrFrame: unknown,
  payload?: unknown,
  meta?: unknown,
  connectionSeq?: unknown,
): DecodedConversationEvent {
  if (arguments.length === 1 && objectValue(eventOrFrame)?.event !== undefined) {
    return decodeValidatedFrame(eventOrFrame)
  }
  const frame: Record<string, unknown> = {
    event: eventOrFrame,
    payload,
  }
  if (arguments.length >= 3) frame.meta = meta
  if (arguments.length >= 4) frame.seq = connectionSeq
  return decodeValidatedFrame(frame)
}

/** Return false for unrelated Gateway events such as `presence` or `tick`. */
export function isConversationEventName(value: unknown): boolean {
  try {
    decodeValidatedFrame({ event: value })
    return true
  } catch {
    return false
  }
}
