import type { RpcCallOptions } from '@/lib/rpc'
import {
  CHAT_HISTORY_METHOD,
  type ChatHistoryMessage,
  type ChatHistoryParams,
  type ChatHistoryResult,
  type CompactionSummary,
  type TurnOutcome,
} from '@/contracts/generated/v4/chatHistory'
import {
  validateChatHistoryParams,
  validateChatHistoryResult,
} from '@/contracts/generated/v4/chatHistoryValidators.mjs'
import type {
  SessionReadCompactionSummary,
  SessionReadHistoryPage,
  SessionReadJsonObject,
  SessionReadMessage,
  SessionReadPortHistoryRequest,
  SessionReadTurnContext,
  SessionReadTurnOutcome,
} from '@/modules/sessionReadLifecycle'

const DEFAULT_HISTORY_BUDGET_MS = 15_000

export interface SessionHistoryV4Transport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

export interface SessionHistoryV4Policy {
  readonly concurrentHistoryReads: () => boolean
  readonly now?: () => number
}

export interface SessionHistoryV4RequestOptions {
  readonly includeSummaries: boolean
  readonly expectedGeneration?: number
  readonly onSent?: (generation: number) => void
  readonly policy: SessionHistoryV4Policy
  readonly contractError: (message: string) => Error
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function camelKey(value: string): string {
  return value.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase())
}

function projectJson(value: unknown): unknown {
  if (Array.isArray(value)) return Object.freeze(value.map(projectJson))
  const item = objectValue(value)
  if (!item) return value
  return Object.freeze(Object.fromEntries(
    Object.entries(item).map(([key, child]) => [camelKey(key), projectJson(child)]),
  ))
}

function projectObject(value: unknown): SessionReadJsonObject | null {
  const item = objectValue(value)
  return item ? projectJson(item) as SessionReadJsonObject : null
}

function projectObjectArray(value: unknown): readonly SessionReadJsonObject[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(value.flatMap(item => {
    const projected = projectObject(item)
    return projected ? [projected] : []
  }))
}

function projectUnknownArray(value: unknown): readonly unknown[] {
  return Object.freeze(Array.isArray(value) ? value.map(projectJson) : [])
}

function additionalFields(
  value: Record<string, unknown>,
  known: ReadonlySet<string>,
): SessionReadJsonObject {
  return Object.freeze(Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !known.has(key))
      .map(([key, child]) => [camelKey(key), projectJson(child)]),
  ))
}

function textValue(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  }
  return null
}

function numberValue(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

function booleanValue(...values: unknown[]): boolean | null {
  for (const value of values) {
    if (typeof value === 'boolean') return value
  }
  return null
}

const TURN_CONTEXT_FIELDS = new Set([
  'turn_id', 'turnId', 'promoted_turn_id', 'promotedTurnId',
  'applied_iteration', 'appliedIteration', 'activity_markers', 'activityMarkers',
])

function projectTurnContext(value: unknown): SessionReadTurnContext | null {
  const raw = objectValue(value)
  if (!raw) return null
  return Object.freeze({
    turnId: textValue(raw.turn_id, raw.turnId),
    promotedTurnId: textValue(raw.promoted_turn_id, raw.promotedTurnId),
    appliedIteration: numberValue(raw.applied_iteration, raw.appliedIteration),
    activityMarkers: projectUnknownArray(raw.activity_markers ?? raw.activityMarkers),
    additional: additionalFields(raw, TURN_CONTEXT_FIELDS),
  })
}

const MESSAGE_FIELDS = new Set([
  'id', 'message_id', 'transcript_id', 'role', 'text', 'timestamp', 'ts',
  'reasoning_content', 'reasoningContent', 'router_decision', 'routerDecision',
  'artifacts', 'tool_calls', 'toolCalls', 'timeline', 'attachments',
  'prompt_annotations', 'promptAnnotations', 'provenance_kind',
  'provenance_source_session_key', 'provenance_source_tool', 'turn_context',
  'turnContext', 'usage', 'turn_usage', 'turnUsage', 'model', 'model_id',
  'input', 'input_tokens', 'inputTokens', 'output', 'output_tokens', 'outputTokens',
])

function projectMessage(value: ChatHistoryMessage, index: number): SessionReadMessage {
  const raw = value as Record<string, unknown>
  const messageId = textValue(value.message_id)
  const transcriptId = textValue(value.transcript_id)
  const usageRaw = objectValue(raw.turn_usage ?? raw.turnUsage ?? raw.usage)
  const reasoning = raw.reasoning_content ?? raw.reasoningContent
  const timestamp = value.timestamp ?? value.ts
  return Object.freeze({
    id: textValue(value.id, messageId, transcriptId) ?? `history:${index}`,
    messageId,
    transcriptId,
    role: textValue(value.role)?.toLowerCase() ?? 'unknown',
    text: typeof value.text === 'string' ? value.text : '',
    createdAt: typeof timestamp === 'string' || typeof timestamp === 'number'
      ? timestamp
      : null,
    // Reasoning can intentionally contain leading/trailing whitespace.
    reasoningContent: typeof reasoning === 'string' ? reasoning : null,
    routerDecision: projectObject(raw.router_decision ?? raw.routerDecision),
    artifacts: projectObjectArray(raw.artifacts),
    toolCalls: projectUnknownArray(raw.tool_calls ?? raw.toolCalls),
    timeline: projectUnknownArray(raw.timeline),
    attachments: projectObjectArray(raw.attachments),
    promptAnnotations: projectUnknownArray(raw.prompt_annotations ?? raw.promptAnnotations),
    provenance: Object.freeze({
      kind: textValue(value.provenance_kind),
      sourceSessionKey: textValue(value.provenance_source_session_key),
      sourceTool: textValue(value.provenance_source_tool),
    }),
    turnContext: projectTurnContext(raw.turn_context ?? raw.turnContext),
    usage: projectObject(usageRaw),
    model: textValue(raw.model, raw.model_id),
    inputTokens: numberValue(
      raw.input_tokens,
      raw.inputTokens,
      raw.input,
      usageRaw?.input_tokens,
      usageRaw?.inputTokens,
      usageRaw?.input,
    ),
    outputTokens: numberValue(
      raw.output_tokens,
      raw.outputTokens,
      raw.output,
      usageRaw?.output_tokens,
      usageRaw?.outputTokens,
      usageRaw?.output,
    ),
    additional: additionalFields(raw, MESSAGE_FIELDS),
  })
}

const COMPACTION_FIELDS = new Set([
  'id', 'compaction_id', 'compaction_index', 'trigger_reason', 'summary_text',
  'summary_format', 'coverage_status', 'removed_count', 'kept_count',
  'covered_through_id', 'created_at',
])

function projectCompaction(value: CompactionSummary): SessionReadCompactionSummary {
  const raw = value as Record<string, unknown>
  return Object.freeze({
    id: textValue(value.id),
    compactionId: textValue(value.compaction_id),
    compactionIndex: numberValue(value.compaction_index),
    triggerReason: textValue(value.trigger_reason),
    summaryText: typeof value.summary_text === 'string' ? value.summary_text : '',
    summaryFormat: typeof value.summary_format === 'string' ? value.summary_format : '',
    coverageStatus: typeof value.coverage_status === 'string' ? value.coverage_status : '',
    removedCount: numberValue(value.removed_count),
    keptCount: numberValue(value.kept_count),
    coveredThroughId: textValue(value.covered_through_id),
    createdAt: numberValue(value.created_at),
    additional: additionalFields(raw, COMPACTION_FIELDS),
  })
}

const TURN_OUTCOME_FIELDS = new Set([
  'turn_id', 'task_id', 'status', 'started_at', 'finished_at', 'outcome',
  'error_class', 'errorClass', 'code', 'retryable', 'activity_snapshot',
  'activitySnapshot', 'usage', 'turn_usage', 'turnUsage', 'usage_call_index',
  'usageCallIndex', 'no_prior_provider_dispatch', 'noPriorProviderDispatch',
  'replay_safe', 'replaySafe', 'retry_after_ms', 'retryAfterMs',
  'user_message_id', 'userMessageId', 'terminal_message', 'terminalMessage',
])

function projectTurnOutcome(value: TurnOutcome): SessionReadTurnOutcome {
  const raw = value as Record<string, unknown>
  const terminalMessage = raw.terminal_message ?? raw.terminalMessage
  return Object.freeze({
    turnId: value.turn_id,
    taskId: textValue(value.task_id),
    status: value.status,
    startedAt: numberValue(value.started_at),
    finishedAt: numberValue(value.finished_at),
    outcome: projectObject(value.outcome) ?? Object.freeze({}),
    errorClass: textValue(raw.error_class, raw.errorClass, raw.code),
    retryable: booleanValue(raw.retryable),
    activitySnapshot: projectObject(raw.activity_snapshot ?? raw.activitySnapshot),
    usage: projectObject(raw.turn_usage ?? raw.turnUsage ?? raw.usage),
    replayProof: Object.freeze({
      usageCallIndex: numberValue(raw.usage_call_index, raw.usageCallIndex),
      noPriorProviderDispatch: booleanValue(
        raw.no_prior_provider_dispatch,
        raw.noPriorProviderDispatch,
      ),
      replaySafe: booleanValue(raw.replay_safe, raw.replaySafe),
      retryAfterMs: numberValue(raw.retry_after_ms, raw.retryAfterMs),
      userMessageId: textValue(raw.user_message_id, raw.userMessageId),
      terminalMessage: typeof terminalMessage === 'string' ? terminalMessage : null,
    }),
    additional: additionalFields(raw, TURN_OUTCOME_FIELDS),
  })
}

const HISTORY_FIELDS = new Set([
  'messages', 'has_more', 'oldest_cursor', 'newest_cursor', 'history_scope',
  'loaded_count', 'page_size', 'canonical_available', 'canonical_complete',
  'compaction_summaries', 'turn_outcomes',
])

interface CanonicalProofPresence {
  readonly canonicalAvailable: boolean | null
  readonly canonicalComplete: boolean | null
}

function projectV4SessionHistory(
  value: ChatHistoryResult,
  proof: CanonicalProofPresence,
): SessionReadHistoryPage {
  return Object.freeze({
    messages: Object.freeze(value.messages.map(projectMessage)),
    hasMore: value.has_more,
    oldestCursor: value.oldest_cursor,
    newestCursor: value.newest_cursor,
    scope: value.history_scope === 'latest_window' ? 'latestWindow' : value.history_scope,
    loadedCount: value.loaded_count,
    pageSize: value.page_size,
    canonicalAvailable: proof.canonicalAvailable,
    canonicalComplete: proof.canonicalComplete,
    compactionSummaries: Object.freeze(value.compaction_summaries.map(projectCompaction)),
    turnOutcomes: Object.freeze(value.turn_outcomes.map(projectTurnOutcome)),
    additional: additionalFields(value as Record<string, unknown>, HISTORY_FIELDS),
  })
}

function withLegacyCanonicalDefaults(value: unknown): {
  readonly value: unknown
  readonly proof: CanonicalProofPresence
} {
  const raw = objectValue(value)
  if (!raw) {
    return {
      value,
      proof: { canonicalAvailable: null, canonicalComplete: null },
    }
  }
  const hasAvailable = raw.canonical_available !== undefined
  const hasComplete = raw.canonical_complete !== undefined
  return {
    value: hasAvailable && hasComplete
      ? value
      : {
          ...raw,
          // Defaults exist only to validate the rest of a legacy result. The
          // domain projection retains null so callers can distinguish missing
          // proof from an explicit false value.
          ...(hasAvailable ? {} : { canonical_available: true }),
          ...(hasComplete ? {} : { canonical_complete: false }),
        },
    proof: {
      canonicalAvailable: hasAvailable && typeof raw.canonical_available === 'boolean'
        ? raw.canonical_available
        : null,
      canonicalComplete: hasComplete && typeof raw.canonical_complete === 'boolean'
        ? raw.canonical_complete
        : null,
    },
  }
}

function timeoutMs(
  request: SessionReadPortHistoryRequest,
  policy: SessionHistoryV4Policy,
): number {
  const now = policy.now?.() ?? Date.now()
  const budget = request.budgetMs ?? DEFAULT_HISTORY_BUDGET_MS
  const deadlineBudget = request.deadlineAt === undefined
    ? budget
    : request.deadlineAt - now
  return Math.max(1, Math.min(budget, deadlineBudget))
}

export async function requestV4SessionHistory(
  rpc: SessionHistoryV4Transport,
  sessionKey: string,
  request: SessionReadPortHistoryRequest,
  options: SessionHistoryV4RequestOptions,
): Promise<SessionReadHistoryPage> {
  const params: ChatHistoryParams = {
    sessionKey,
    limit: request.limit,
    includeCanonical: true,
    includeSummaries: options.includeSummaries,
    ...(request.direction === 'before' ? { before: request.cursor } : {}),
    ...(request.direction === 'after' ? { after: request.cursor } : {}),
  }
  if (!validateChatHistoryParams(params)) {
    throw options.contractError(`${CHAT_HISTORY_METHOD} params`)
  }
  const callOptions: RpcCallOptions = {
    signal: request.signal,
    timeoutMs: timeoutMs(request, options.policy),
    timeoutAction: options.policy.concurrentHistoryReads() ? 'reject' : 'reconnect',
    abortAction: 'reject',
    ...(options.expectedGeneration === undefined
      ? {}
      : { expectedGeneration: options.expectedGeneration }),
    ...(options.onSent ? { onSent: options.onSent } : {}),
  }
  const normalized = withLegacyCanonicalDefaults(await rpc.request(
    CHAT_HISTORY_METHOD,
    params,
    callOptions,
  ))
  if (!validateChatHistoryResult(normalized.value)) {
    throw options.contractError(`${CHAT_HISTORY_METHOD} result`)
  }
  return projectV4SessionHistory(
    normalized.value as ChatHistoryResult,
    normalized.proof,
  )
}
