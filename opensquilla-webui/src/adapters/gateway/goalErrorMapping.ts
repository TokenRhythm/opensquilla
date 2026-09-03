import type { GoalCenterError, GoalCenterFailureReason } from '@/modules/goalCenter'
import { GoalCenterError as GoalCenterErrorClass } from '@/modules/goalCenter'

type JsonObject = Record<string, unknown>

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function codeOf(error: unknown): string {
  const source = objectValue(error)
  const data = objectValue(source?.data)
  return String(source?.code ?? data?.code ?? '').toUpperCase()
}

const GOAL_FAILURE_REASONS: Readonly<Record<string, GoalCenterFailureReason>> = {
  INVALID_GOAL_OBJECTIVE: 'invalid-objective',
  INVALID_GOAL_COMMAND: 'invalid-command',
  INVALID_GOAL_PROGRESS: 'invalid-command',
  INVALID_GOAL_REASON: 'invalid-command',
  INVALID_GOAL_GUARDRAIL: 'invalid-command',
  GOAL_NOT_FOUND: 'not-found',
  SESSION_GENERATION_CHANGED: 'session-changed',
  STALE_GOAL: 'changed',
  GOAL_ACTIVE: 'already-active',
  GOAL_BUSY: 'busy',
  GOAL_NOT_RESUMABLE: 'not-resumable',
  GOAL_EXECUTION_DISABLED: 'execution-disabled',
  EXECUTION_LEASE_REQUIRED: 'connection-required',
  PLAN_MODE_ACTIVE: 'plan-mode-active',
  PLAN_RUN_ACTIVE: 'plan-run-active',
  IDEMPOTENCY_CONFLICT: 'request-conflict',
}

/** Keep historical Gateway error codes behind the Goal domain boundary. */
export function mapGoalError(
  error: unknown,
  options: { reattach?: boolean } = {},
): GoalCenterError {
  if (error instanceof GoalCenterErrorClass) return error
  const code = codeOf(error)
  const source = objectValue(error)
  const data = objectValue(source?.data)
  const message = typeof source?.message === 'string'
    ? source.message
    : error instanceof Error
      ? error.message
      : 'Goal request failed'
  const details = source?.details ?? data?.details
  const reason = GOAL_FAILURE_REASONS[code]
  if (code === 'NOT_FOUND' || code === 'SESSION_NOT_FOUND') {
    return new GoalCenterErrorClass('not-found', message, { details, cause: error })
  }
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') {
    return new GoalCenterErrorClass('unsupported', message, { details, cause: error })
  }
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN') {
    return new GoalCenterErrorClass('forbidden', message, { details, cause: error })
  }
  if (code === 'GOAL_ACTIVE' || code === 'GOAL_BUSY' || code === 'SESSION_GENERATION_CHANGED'
    || code === 'PLAN_MODE_ACTIVE' || code === 'PLAN_RUN_ACTIVE' || code === 'IDEMPOTENCY_CONFLICT'
    || code === 'GOAL_NOT_FOUND' || code === 'GOAL_NOT_RESUMABLE'
    || (options.reattach && code === 'EXECUTION_LEASE_REQUIRED')
    || code === 'CONFLICT' || code === 'STALE_GOAL') {
    return new GoalCenterErrorClass('conflict', message, {
      reason,
      details,
      retryable: true,
      cause: error,
    })
  }
  if (code === 'GOAL_EXECUTION_DISABLED') {
    return new GoalCenterErrorClass('unsupported', message, { reason, details, cause: error })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS' || reason === 'invalid-command'
    || reason === 'invalid-objective') {
    return new GoalCenterErrorClass('invalid', message, { reason, details, cause: error })
  }
  return new GoalCenterErrorClass('unavailable', message, { details, cause: error })
}
