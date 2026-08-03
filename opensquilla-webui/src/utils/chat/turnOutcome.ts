import type { ChatRunTask, ChatTurnOutcome } from '@/types/chat'
import type { ChatHistoryTurnOutcome } from '@/types/rpc'

type RawOutcomeRecord = Record<string, unknown>

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function bool(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
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
