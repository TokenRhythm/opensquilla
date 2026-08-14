import type { ChatRunTask, ChatTurnOutcome } from '@/types/chat'
import type { ChatHistoryTurnOutcome } from '@/types/rpc'
import {
  isUsageAccountingBarrier,
  terminalActivityStatusHistory,
  usageAccountingErrorCode,
} from '@/utils/chat/usageAccountingFailure'

type RawOutcomeRecord = Record<string, unknown>

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function bool(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

type ConsistentField<T> = {
  present: boolean
  valid: boolean
  value?: T
}

function fieldState<T>(
  source: RawOutcomeRecord,
  keys: readonly string[],
  parse: (value: unknown) => T | undefined,
): ConsistentField<T> {
  const values = keys
    .filter(key => Object.prototype.hasOwnProperty.call(source, key) && source[key] != null)
    .map(key => parse(source[key]))
  if (!values.length) return { present: false, valid: false }
  if (values.some(value => value === undefined)) return { present: true, valid: false }
  const first = values[0] as T
  return values.every(value => value === first)
    ? { present: true, valid: true, value: first }
    : { present: true, valid: false }
}

function mergedFieldState<T>(
  outer: ConsistentField<T>,
  nested: ConsistentField<T>,
): ConsistentField<T> {
  if (!outer.present) return nested
  if (!nested.present) return outer
  if (!outer.valid || !nested.valid || outer.value !== nested.value) {
    return { present: true, valid: false }
  }
  return outer
}

function nonEmptyText(value: unknown): string | undefined {
  const normalized = text(value)
  return normalized || undefined
}

function positiveInteger(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
    ? value
    : undefined
}

function outcomeBody(raw: unknown): RawOutcomeRecord {
  return raw && typeof raw === 'object' && !Array.isArray(raw)
    ? raw as RawOutcomeRecord
    : {}
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
  const turnIdState = mergedFieldState(
    fieldState(record, ['turn_id', 'turnId'], nonEmptyText),
    fieldState(nested, ['turn_id', 'turnId'], nonEmptyText),
  )
  const turnId = turnIdState.valid
    ? turnIdState.value || ''
    : text(record.turn_id ?? record.turnId ?? nested.turn_id ?? nested.turnId)
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
  const noPriorState = mergedFieldState(
    fieldState(
      record,
      ['no_prior_provider_dispatch', 'noPriorProviderDispatch'],
      bool,
    ),
    fieldState(
      nested,
      ['no_prior_provider_dispatch', 'noPriorProviderDispatch'],
      bool,
    ),
  )
  const replaySafeState = mergedFieldState(
    fieldState(record, ['replay_safe', 'replaySafe'], bool),
    fieldState(nested, ['replay_safe', 'replaySafe'], bool),
  )
  const userMessageIdState = mergedFieldState(
    fieldState(record, ['user_message_id', 'userMessageId'], nonEmptyText),
    fieldState(nested, ['user_message_id', 'userMessageId'], nonEmptyText),
  )
  const explicitErrorCodes = [
    record.code,
    record.error_class,
    record.errorClass,
    nested.code,
    nested.error_class,
    nested.errorClass,
  ].map(text).filter(Boolean)
  const errorCodes = explicitErrorCodes.length
    ? explicitErrorCodes
    : [record.reason, nested.reason].map(text).filter(Boolean)
  const barrierCodes = errorCodes.filter(isUsageAccountingBarrier)
  const barrierCodeConflict = barrierCodes.length > 0 && new Set(errorCodes).size > 1
  const errorClass = barrierCodes[0] || text(
    record.error_class
    ?? record.errorClass
    ?? nested.error_class
    ?? nested.errorClass
    ?? usageAccountingErrorCode(record),
  )
  const terminalMessage = text(
    record.terminal_message
    ?? record.terminalMessage
    ?? nested.terminal_message
    ?? nested.terminalMessage,
  )
  const retryAfterRaw = record.retry_after_ms ?? record.retryAfterMs
    ?? nested.retry_after_ms ?? nested.retryAfterMs
  const retryAfter = Number(retryAfterRaw)
  const usageCallIndexState = mergedFieldState(
    fieldState(record, ['usage_call_index', 'usageCallIndex'], positiveInteger),
    fieldState(nested, ['usage_call_index', 'usageCallIndex'], positiveInteger),
  )
  const usageCallIndex = usageCallIndexState.valid
    ? usageCallIndexState.value
    : undefined
  const hasUsageReplayProof = usageCallIndexState.present
    || noPriorState.present
    || replaySafeState.present
  const replayConflict = !turnIdState.valid
    || (userMessageIdState.present && !userMessageIdState.valid)
    || barrierCodeConflict
    || (usageCallIndexState.present && !usageCallIndexState.valid)
    || (noPriorState.present && !noPriorState.valid)
    || (replaySafeState.present && !replaySafeState.valid)
  const provedNoPriorProviderDispatch = !replayConflict
    && usageCallIndex === 1
    && noPriorState.value === true
  const provedReplaySafe = provedNoPriorProviderDispatch
    && replaySafeState.value === true
  const statusHistory = terminalActivityStatusHistory(
    record.activity_snapshot ?? record.activitySnapshot
      ?? nested.activity_snapshot ?? nested.activitySnapshot,
    turnId,
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
    ...(hasUsageReplayProof
      ? { noPriorProviderDispatch: provedNoPriorProviderDispatch }
      : {}),
    ...(hasUsageReplayProof ? { replaySafe: provedReplaySafe } : {}),
    ...(userMessageIdState.valid && userMessageIdState.value
      ? { userMessageId: userMessageIdState.value }
      : {}),
    ...(errorClass ? { errorClass } : {}),
    ...(terminalMessage ? { terminalMessage } : {}),
    ...(Number.isFinite(retryAfter) && retryAfter > 0 ? { retryAfterMs: retryAfter } : {}),
    ...(usageCallIndex !== undefined ? { usageCallIndex } : {}),
    ...(statusHistory.length ? { statusHistory } : {}),
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
