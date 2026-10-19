import type { GoalCenterError, GoalCenterFailureReason, GoalRecoveryAction } from '@/modules/goalCenter'
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
  const detailObject = objectValue(details)
  const rawAction = detailObject?.recoveryAction
  const recoveryAction = typeof rawAction === 'string'
    && ['retry', 'await-subscription', 'require-takeover', 'owner-attached', 'refresh-goal', 'stop'].includes(rawAction)
    ? rawAction as GoalRecoveryAction : undefined
  const rawRetryable = source?.retryable ?? data?.retryable
  const retryable = typeof rawRetryable === 'boolean' ? rawRetryable : undefined
  const rawRetryAfter = source?.retry_after_ms ?? source?.retryAfterMs ?? data?.retry_after_ms ?? data?.retryAfterMs
  const retryAfterMs = typeof rawRetryAfter === 'number' && Number.isFinite(rawRetryAfter)
    ? Math.max(0, rawRetryAfter) : 0
  const policy = { details, recoveryAction, retryable, retryAfterMs, cause: error }
  const reason = GOAL_FAILURE_REASONS[code]
  if (code === 'NOT_FOUND' || code === 'SESSION_NOT_FOUND') {
    return new GoalCenterErrorClass('not-found', message, policy)
  }
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') {
    return new GoalCenterErrorClass('unsupported', message, policy)
  }
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN') {
    return new GoalCenterErrorClass('forbidden', message, policy)
  }
  if (code === 'GOAL_ACTIVE' || code === 'GOAL_BUSY' || code === 'SESSION_GENERATION_CHANGED'
    || code === 'PLAN_MODE_ACTIVE' || code === 'PLAN_RUN_ACTIVE' || code === 'IDEMPOTENCY_CONFLICT'
    || code === 'GOAL_NOT_FOUND' || code === 'GOAL_NOT_RESUMABLE'
    || (options.reattach && code === 'EXECUTION_LEASE_REQUIRED')
    || code === 'CONFLICT' || code === 'STALE_GOAL') {
    return new GoalCenterErrorClass('conflict', message, {
      reason,
      ...policy,
      // A server rejection is not permission to repeat an invalid token. Old
      // lease errors have no safe transient interpretation without an action.
      retryable: retryable ?? (code === 'EXECUTION_LEASE_REQUIRED' ? false : true),
    })
  }
  if (code === 'GOAL_EXECUTION_DISABLED') {
    return new GoalCenterErrorClass('unsupported', message, { reason, ...policy })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS' || reason === 'invalid-command'
    || reason === 'invalid-objective') {
    return new GoalCenterErrorClass('invalid', message, { reason, ...policy })
  }
  const transient = ['UNAVAILABLE', 'STORAGE_BUSY', 'RPC_TIMEOUT', 'RPC_TRANSPORT_ERROR'].includes(code)
  return new GoalCenterErrorClass('unavailable', message, { ...policy, retryable: retryable ?? transient })
}
