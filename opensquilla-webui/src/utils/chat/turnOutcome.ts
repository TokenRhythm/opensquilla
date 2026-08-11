import type {
  ChatRunTask,
  ChatTurnOutcome,
  DocumentMutationOutcome,
  DocumentMutationPhase,
  DocumentMutationRetryPolicy,
  DocumentMutationStatus,
} from '@/types/chat'
import type { ChatHistoryTurnOutcome } from '@/types/rpc'

type RawOutcomeRecord = Record<string, unknown>

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function bool(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

function finiteInteger(value: unknown): number | undefined {
  const numeric = Number(value)
  return Number.isInteger(numeric) && numeric >= 0 ? numeric : undefined
}

function outcomeBody(raw: unknown): RawOutcomeRecord {
  return raw && typeof raw === 'object' && !Array.isArray(raw)
    ? raw as RawOutcomeRecord
    : {}
}

const DOCUMENT_MUTATION_STATUSES = new Set<DocumentMutationStatus>([
  'not_attempted',
  'applied',
  'not_applied',
  'conflict',
  'ambiguous',
])
const DOCUMENT_MUTATION_PHASES = new Set<DocumentMutationPhase>(['proposal', 'commit'])
const DOCUMENT_MUTATION_RETRY_POLICIES = new Set<DocumentMutationRetryPolicy>([
  'same_turn',
  'new_turn',
  'refresh',
  'reconcile',
  'never',
])

function enumValue<T extends string>(value: unknown, accepted: ReadonlySet<T>): T | undefined {
  const normalized = text(value) as T
  return accepted.has(normalized) ? normalized : undefined
}

export function normalizeDocumentMutationOutcome(
  raw: unknown,
): DocumentMutationOutcome | undefined {
  const record = outcomeBody(raw)
  const status = enumValue(record.status, DOCUMENT_MUTATION_STATUSES)
  if (!status) return undefined
  const phase = enumValue(
    record.phase ?? record.failure_phase ?? record.failurePhase,
    DOCUMENT_MUTATION_PHASES,
  )
  const retryPolicy = enumValue(
    record.retry_policy ?? record.retryPolicy,
    DOCUMENT_MUTATION_RETRY_POLICIES,
  )
  const version = finiteInteger(record.version ?? record.schema_version ?? record.schemaVersion) ?? 1
  const code = text(record.code ?? record.failure_code ?? record.failureCode)
  const attemptId = text(record.attempt_id ?? record.attemptId)
  const changeSetId = text(record.change_set_id ?? record.changeSetId)
  const resultRevisionId = text(
    record.result_revision_id ?? record.resultRevisionId ?? record.revision_id ?? record.revisionId,
  )
  const proposalAttempts = finiteInteger(record.proposal_attempts ?? record.proposalAttempts)
  const corrected = bool(record.corrected)
  return {
    version,
    status,
    ...(phase ? { phase } : {}),
    ...(code ? { code } : {}),
    ...(retryPolicy ? { retryPolicy } : {}),
    ...(attemptId ? { attemptId } : {}),
    ...(changeSetId ? { changeSetId } : {}),
    ...(resultRevisionId ? { resultRevisionId } : {}),
    ...(proposalAttempts !== undefined ? { proposalAttempts } : {}),
    ...(corrected !== undefined ? { corrected } : {}),
  }
}

function timestampMilliseconds(value: number | string | undefined): number {
  if (value == null) return Number.NaN
  const numeric = typeof value === 'number' ? value : Number(value)
  if (Number.isFinite(numeric)) {
    return numeric < 100_000_000_000 ? numeric * 1_000 : numeric
  }
  return typeof value === 'string' ? Date.parse(value) : Number.NaN
}

export function normalizeTurnOutcome(
  raw: ChatHistoryTurnOutcome | ChatRunTask | Record<string, unknown> | null | undefined,
): ChatTurnOutcome | undefined {
  if (!raw) return undefined
  const record = raw as RawOutcomeRecord
  const nested = outcomeBody(record.outcome ?? record.turn_outcome ?? record.turnOutcome)
  const turnId = text(record.turn_id ?? record.turnId ?? nested.turn_id ?? nested.turnId)
  if (!turnId) return undefined
  const taskId = text(record.task_id ?? record.taskId ?? nested.task_id ?? nested.taskId)
  const status = text(record.status ?? nested.status ?? nested.kind)
  const kind = text(record.kind ?? nested.kind)
  const reason = text(record.reason ?? nested.reason)
  const cancellationSource = text(
    record.cancellation_source
    ?? record.cancellationSource
    ?? nested.cancellation_source
    ?? nested.cancellationSource,
  )
  const startedAt = record.started_at ?? record.startedAt ?? nested.started_at ?? nested.startedAt
  const finishedAt = record.finished_at ?? record.finishedAt ?? nested.finished_at ?? nested.finishedAt
  const retryable = bool(record.retryable ?? nested.retryable)
  const documentMutationOutcome = normalizeDocumentMutationOutcome(
    record.document_mutation_outcome
    ?? record.documentMutationOutcome
    ?? record.document_mutation
    ?? record.documentMutation
    ?? nested.document_mutation_outcome
    ?? nested.documentMutationOutcome
    ?? nested.document_mutation
    ?? nested.documentMutation,
  )
  return {
    turnId,
    ...(taskId ? { taskId } : {}),
    status,
    ...(kind ? { kind } : {}),
    ...(reason ? { reason } : {}),
    ...(cancellationSource ? { cancellationSource } : {}),
    ...(startedAt != null ? { startedAt: startedAt as string | number } : {}),
    ...(finishedAt != null ? { finishedAt: finishedAt as string | number } : {}),
    ...(retryable !== undefined ? { retryable } : {}),
    ...(documentMutationOutcome ? { documentMutationOutcome } : {}),
  }
}

export type TurnOutcomePresentation =
  | 'completed'
  | 'stopped'
  | 'interrupted'
  | 'timeout'
  | 'failed'

export function turnOutcomePresentation(
  outcome: ChatTurnOutcome | null | undefined,
): TurnOutcomePresentation {
  const status = text(outcome?.status).toLowerCase()
  const kind = text(outcome?.kind).toLowerCase()
  const source = text(outcome?.cancellationSource).toLowerCase()
  if (status === 'timeout' || kind === 'timeout') return 'timeout'
  if (status === 'failed' || kind === 'failed' || kind === 'error') return 'failed'
  if (
    source === 'webui_stop'
    || source === 'webui_escape'
    || kind === 'user_stopped'
    || kind === 'stopped'
  ) return 'stopped'
  if (
    ['cancelled', 'canceled', 'interrupted', 'abandoned', 'killed'].includes(status)
    || ['cancelled', 'canceled', 'interrupted', 'abandoned', 'killed'].includes(kind)
  ) return 'interrupted'
  return 'completed'
}

export function turnOutcomeDurationSeconds(
  outcome: ChatTurnOutcome | null | undefined,
): number {
  if (outcome?.startedAt == null || outcome.finishedAt == null) return 0
  const start = timestampMilliseconds(outcome.startedAt)
  const finish = timestampMilliseconds(outcome.finishedAt)
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return 0
  return Math.max(1, Math.round((finish - start) / 1_000))
}
