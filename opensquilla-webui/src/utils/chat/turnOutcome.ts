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
    // Presence is authoritative for replay-safety fields. Explicit null (or
    // undefined) is not the same as omission and must invalidate the proof.
    .filter(key => Object.prototype.hasOwnProperty.call(source, key))
    .map(key => parse(source[key]))
  if (!values.length) return { present: false, valid: false }
  if (values.some(value => value === undefined)) return { present: true, valid: false }
  const first = values[0] as T
  return values.every(value => value === first)
    ? { present: true, valid: true, value: first }
    : { present: true, valid: false }
}

function mergedFieldStates<T>(states: readonly ConsistentField<T>[]): ConsistentField<T> {
  let merged: ConsistentField<T> = { present: false, valid: false }
  for (const state of states) {
    if (!state.present) continue
    if (!merged.present) {
      merged = state
      continue
    }
    if (!merged.valid || !state.valid || merged.value !== state.value) {
      return { present: true, valid: false }
    }
  }
  return merged
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

type OutcomeContainers = {
  records: RawOutcomeRecord[]
  invalid: boolean
}

function outcomeContainers(record: RawOutcomeRecord): OutcomeContainers {
  const records: RawOutcomeRecord[] = []
  let invalid = false
  for (const key of ['outcome', 'turn_outcome', 'turnOutcome'] as const) {
    if (!Object.prototype.hasOwnProperty.call(record, key)) continue
    const value = record[key]
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      invalid = true
      continue
    }
    records.push(value as RawOutcomeRecord)
  }
  return { records, invalid }
}

function fieldStateAcross<T>(
  sources: readonly RawOutcomeRecord[],
  keys: readonly string[],
  parse: (value: unknown) => T | undefined,
): ConsistentField<T> {
  return mergedFieldStates(sources.map(source => fieldState(source, keys, parse)))
}

function firstTextValue(
  sources: readonly RawOutcomeRecord[],
  keys: readonly string[],
): string {
  for (const source of sources) {
    for (const key of keys) {
      if (!Object.prototype.hasOwnProperty.call(source, key)) continue
      const value = text(source[key])
      if (value) return value
    }
  }
  return ''
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
  const containers = outcomeContainers(record)
  const nested = containers.records[0] ?? {}
  const sources = [record, ...containers.records]
  const turnIdState = fieldStateAcross(sources, ['turn_id', 'turnId'], nonEmptyText)
  const turnId = turnIdState.valid
    ? turnIdState.value || ''
    : firstTextValue(sources, ['turn_id', 'turnId'])
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
  const noPriorState = fieldStateAcross(
    sources,
    ['no_prior_provider_dispatch', 'noPriorProviderDispatch'],
    bool,
  )
  const replaySafeState = fieldStateAcross(
    sources,
    ['replay_safe', 'replaySafe'],
    bool,
  )
  const userMessageIdState = fieldStateAcross(
    sources,
    ['user_message_id', 'userMessageId'],
    nonEmptyText,
  )
  const errorCodeState = fieldStateAcross(
    sources,
    ['code', 'error_class', 'errorClass'],
    nonEmptyText,
  )
  const explicitErrorCodes = sources.flatMap(source => [
    source.code,
    source.error_class,
    source.errorClass,
  ]).map(text).filter(Boolean)
  const errorCodes = explicitErrorCodes.length
    ? explicitErrorCodes
    : sources.map(source => source.reason).map(text).filter(Boolean)
  const barrierCodes = errorCodes.filter(isUsageAccountingBarrier)
  const barrierCodeConflict = barrierCodes.length > 0
    && (
      (errorCodeState.present && !errorCodeState.valid)
      || new Set(errorCodes).size > 1
    )
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
  const usageCallIndexState = fieldStateAcross(
    sources,
    ['usage_call_index', 'usageCallIndex'],
    positiveInteger,
  )
  const usageCallIndex = usageCallIndexState.valid
    ? usageCallIndexState.value
    : undefined
  const hasUsageReplayProof = usageCallIndexState.present
    || noPriorState.present
    || replaySafeState.present
  const invalidMixedEnvelope = containers.invalid
    && (
      containers.records.length > 0
      || hasUsageReplayProof
      || userMessageIdState.present
      || barrierCodes.length > 0
    )
  const replayConflict = !turnIdState.valid
    || (userMessageIdState.present && !userMessageIdState.valid)
    || barrierCodeConflict
    || invalidMixedEnvelope
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
  const acceptedRoutingModeRaw = text(
    record.accepted_routing_mode
    ?? record.acceptedRoutingMode
    ?? nested.accepted_routing_mode
    ?? nested.acceptedRoutingMode,
  ).toLowerCase()
  const acceptedRoutingMode = (
    acceptedRoutingModeRaw === 'direct'
    || acceptedRoutingModeRaw === 'router'
    || acceptedRoutingModeRaw === 'ensemble'
  )
    ? acceptedRoutingModeRaw
    : undefined
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
    ...(userMessageIdState.valid && userMessageIdState.value && !replayConflict
      ? { userMessageId: userMessageIdState.value }
      : {}),
    ...(errorClass ? { errorClass } : {}),
    ...(terminalMessage ? { terminalMessage } : {}),
    ...(Number.isFinite(retryAfter) && retryAfter > 0 ? { retryAfterMs: retryAfter } : {}),
    ...(usageCallIndex !== undefined ? { usageCallIndex } : {}),
    ...(statusHistory.length ? { statusHistory } : {}),
    ...(acceptedRoutingMode ? { acceptedRoutingMode } : {}),
  }
}

export type TurnOutcomePresentation =
  | 'completed'
  | 'stopped'
  | 'interrupted'
  | 'timeout'
  | 'failed'

export function isProcessRestartOutcome(
  outcome: ChatTurnOutcome | null | undefined,
): boolean {
  return outcome?.reason === 'process_restart'
    || outcome?.errorClass === 'process_restart'
}

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
