import type { GoalSnapshot } from '@/modules/goalCenter'
import { canonicalSessionKey } from '@/utils/chat/sessionKeys'

type JsonObject = Record<string, unknown>

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function text(...values: unknown[]): string | undefined {
  const found = values
    .filter(value => typeof value === 'string' && value.trim())
    .map(value => (value as string).trim())
  const unique = new Set(found)
  if (unique.size > 1) {
    throw new Error('Goal snapshot contains conflicting string aliases')
  }
  return found[0]
}

function integer(...values: unknown[]): number | undefined {
  const found = values.filter(value => (
    typeof value === 'number' && Number.isInteger(value) && value >= 0
  )) as number[]
  if (new Set(found).size > 1) {
    throw new Error('Goal snapshot contains conflicting numeric aliases')
  }
  return found[0]
}

function nullableText(...values: unknown[]): string | null {
  const present = values.filter(value => value !== undefined)
  if (present.some(value => value !== null && typeof value !== 'string')) {
    throw new Error('Goal snapshot contains an invalid nullable string alias')
  }
  const normalized = present.map(value => value === null ? null : (value as string).trim())
  if (normalized.some(value => value !== null && !value)) {
    throw new Error('Goal snapshot contains a blank nullable string alias')
  }
  const unique = new Set(normalized)
  if (unique.size > 1) {
    throw new Error('Goal snapshot contains conflicting nullable aliases')
  }
  return normalized[0] ?? null
}

/** Project all historical Goal aliases once at the Gateway adapter boundary. */
export function projectGoalSnapshot(value: unknown): GoalSnapshot | null {
  const source = objectValue(value)
  if (!source || typeof source.status !== 'string') return null
  const sessionKey = text(source.sessionKey, source.session_key)
  return {
    goalId: text(source.goalId, source.goal_id),
    sessionKey: sessionKey ? canonicalSessionKey(sessionKey) : undefined,
    sessionId: text(source.sessionId, source.session_id),
    epoch: integer(source.epoch, source.sessionEpoch, source.session_epoch),
    objective: text(source.objective, source.goalText, source.goal_text),
    status: source.status,
    stateRevision: integer(source.stateRevision, source.state_revision),
    objectiveRevision: integer(source.objectiveRevision, source.objective_revision),
    progressRevision: integer(source.progressRevision, source.progress_revision),
    progress: source.progress,
    continuationSeq: integer(source.continuationSeq, source.continuation_seq),
    activeTaskId: nullableText(source.activeTaskId, source.active_task_id),
    executionState: text(source.executionState, source.execution_state),
    createdAt: integer(source.createdAt, source.created_at),
    updatedAt: integer(source.updatedAt, source.updated_at),
    finishedAt: integer(source.finishedAt, source.finished_at) ?? null,
    sourceMessageId: nullableText(
      source.sourceMessageId,
      source.source_message_id,
      source.source_user_message_id,
    ),
    terminalTurnId: nullableText(
      source.terminalTurnId,
      source.terminal_turn_id,
      source.terminal_task_id,
    ),
    continuationDeferredReason: nullableText(
      source.continuationDeferredReason,
      source.continuation_deferred_reason,
    ),
    turnsStarted: integer(source.turnsStarted, source.turns_started),
    turnsSettled: integer(source.turnsSettled, source.turns_settled),
    windowTurnsStarted: integer(source.windowTurnsStarted, source.window_turns_started),
    activeTimeMs: integer(source.activeTimeMs, source.active_time_ms),
    windowActiveTimeMs: integer(source.windowActiveTimeMs, source.window_active_time_ms),
    usage: source.usage,
    pauseReason: nullableText(source.pauseReason, source.pause_reason),
    blockedReason: nullableText(source.blockedReason, source.blocked_reason),
    terminalReason: nullableText(source.terminalReason, source.terminal_reason),
  }
}
