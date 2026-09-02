import type { GoalCenterError } from '@/modules/goalCenter'
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
      details,
      retryable: true,
      cause: error,
    })
  }
  if (code === 'GOAL_EXECUTION_DISABLED') {
    return new GoalCenterErrorClass('unsupported', message, { details, cause: error })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS' || code === 'INVALID_GOAL_COMMAND') {
    return new GoalCenterErrorClass('invalid', message, { details, cause: error })
  }
  return new GoalCenterErrorClass('unavailable', message, { details, cause: error })
}
