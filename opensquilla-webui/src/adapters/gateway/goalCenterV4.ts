import type { RpcCallOptions } from '@/lib/rpc'
import { GOALS_SET_METHOD, type Params as GoalSetParams, type Result as GoalSetWireResult } from '@/contracts/generated/v4/goalsSet'
import { validateParams as validateGoalSetParams, validateResult as validateGoalSetResult } from '@/contracts/generated/v4/goalsSetValidators.mjs'
import { GOALS_STATUS_METHOD, type Params as GoalStatusParams, type Result as GoalStatusWireResult } from '@/contracts/generated/v4/goalsStatus'
import { validateParams as validateGoalStatusParams, validateResult as validateGoalStatusResult } from '@/contracts/generated/v4/goalsStatusValidators.mjs'
import type { GoalCenter, GoalSetResult, GoalSnapshot, GoalStatusResult } from '@/modules/goalCenter'
import { GoalCenterError } from '@/modules/goalCenter'

interface GoalCenterTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports?(method: string): boolean
}

type JsonObject = Record<string, unknown>
const objectValue = (value: unknown): JsonObject | null => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
const text = (...values: unknown[]): string | undefined => values.find(value => typeof value === 'string' && value.trim()) as string | undefined
const integer = (...values: unknown[]): number | undefined => values.find(value => typeof value === 'number' && Number.isInteger(value) && value >= 0) as number | undefined

function snapshot(value: unknown): GoalSnapshot | null {
  const source = objectValue(value)
  if (!source || typeof source.status !== 'string') return null
  return {
    goalId: text(source.goalId, source.goal_id), sessionKey: text(source.sessionKey, source.session_key), sessionId: text(source.sessionId, source.session_id),
    epoch: integer(source.epoch), objective: text(source.objective), status: source.status,
    stateRevision: integer(source.stateRevision, source.state_revision), objectiveRevision: integer(source.objectiveRevision, source.objective_revision),
    progressRevision: integer(source.progressRevision, source.progress_revision), progress: source.progress, continuationSeq: integer(source.continuationSeq, source.continuation_seq),
    activeTaskId: typeof source.activeTaskId === 'string' ? source.activeTaskId : typeof source.active_task_id === 'string' ? source.active_task_id : null,
    executionState: text(source.executionState, source.execution_state), createdAt: integer(source.createdAt, source.created_at), updatedAt: integer(source.updatedAt, source.updated_at),
    finishedAt: integer(source.finishedAt, source.finished_at) ?? null,
    sourceMessageId: text(source.sourceMessageId, source.source_message_id, source.source_user_message_id) ?? null,
    terminalTurnId: text(source.terminalTurnId, source.terminal_turn_id, source.terminal_task_id) ?? null,
    continuationDeferredReason: text(source.continuationDeferredReason, source.continuation_deferred_reason) ?? null,
    turnsStarted: integer(source.turnsStarted, source.turns_started), turnsSettled: integer(source.turnsSettled, source.turns_settled), windowTurnsStarted: integer(source.windowTurnsStarted, source.window_turns_started),
    activeTimeMs: integer(source.activeTimeMs, source.active_time_ms), windowActiveTimeMs: integer(source.windowActiveTimeMs, source.window_active_time_ms), usage: source.usage,
    pauseReason: text(source.pauseReason, source.pause_reason) ?? null, blockedReason: text(source.blockedReason, source.blocked_reason) ?? null, terminalReason: text(source.terminalReason, source.terminal_reason) ?? null,
  }
}

function codeOf(error: unknown): string {
  const source = objectValue(error); const data = objectValue(source?.data)
  return String(source?.code ?? data?.code ?? '').toUpperCase()
}

function mapError(error: unknown): GoalCenterError {
  if (error instanceof GoalCenterError) return error
  const code = codeOf(error); const source = objectValue(error); const data = objectValue(source?.data)
  const message = typeof source?.message === 'string' ? source.message : error instanceof Error ? error.message : 'Goal request failed'
  const details = source?.details ?? data?.details
  if (code === 'NOT_FOUND' || code === 'SESSION_NOT_FOUND') return new GoalCenterError('not-found', message, { details, cause: error })
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') return new GoalCenterError('unsupported', message, { details, cause: error })
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN') return new GoalCenterError('forbidden', message, { details, cause: error })
  if (code === 'GOAL_ACTIVE' || code === 'GOAL_BUSY' || code === 'SESSION_GENERATION_CHANGED' || code === 'PLAN_MODE_ACTIVE' || code === 'PLAN_RUN_ACTIVE' || code === 'IDEMPOTENCY_CONFLICT' || code === 'GOAL_NOT_FOUND' || code === 'GOAL_NOT_RESUMABLE' || code === 'CONFLICT' || code === 'STALE_GOAL') return new GoalCenterError('conflict', message, { details, retryable: true, cause: error })
  if (code === 'GOAL_EXECUTION_DISABLED') return new GoalCenterError('unsupported', message, { details, cause: error })
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS' || code === 'INVALID_GOAL_COMMAND') return new GoalCenterError('invalid', message, { details, cause: error })
  return new GoalCenterError('unavailable', message, { details, cause: error })
}

function optionsFor(signal: AbortSignal | undefined): RpcCallOptions | undefined { return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined }

export function createV4GoalCenter(transport: GoalCenterTransport): GoalCenter {
  return {
    available(operation = 'status') {
      if (!transport.supports) return true
      if (operation === 'goal-mode') {
        return transport.supports(GOALS_SET_METHOD) && transport.supports('goals.capabilities')
      }
      return transport.supports(operation === 'set' ? GOALS_SET_METHOD : GOALS_STATUS_METHOD)
    },
    async status(sessionKey, options): Promise<GoalStatusResult> {
      const params: GoalStatusParams = { sessionKey }
      if (!validateGoalStatusParams(params)) throw new GoalCenterError('invalid', 'goals.status params violated Contract')
      try {
        const raw = await transport.request<GoalStatusWireResult>(GOALS_STATUS_METHOD, params, optionsFor(options?.signal))
        const normalized = {
          ...raw,
          sessionKey: raw.sessionKey ?? raw.session_key,
          sessionId: raw.sessionId ?? raw.session_id,
        }
        if (!validateGoalStatusResult(normalized)) throw new Error('goals.status returned an invalid response')
        if (typeof normalized.sessionKey !== 'string' || typeof normalized.sessionId !== 'string' || !Number.isInteger(normalized.epoch) || !Object.prototype.hasOwnProperty.call(normalized, 'goal')) {
          throw new Error('goals.status response is missing its session fence')
        }
        return { sessionKey: normalized.sessionKey, sessionId: normalized.sessionId, epoch: normalized.epoch, goal: snapshot(normalized.goal) }
      } catch (error) { throw mapError(error) }
    },
    async set(input, options): Promise<GoalSetResult> {
      const params: GoalSetParams = { sessionKey: input.sessionKey, objective: input.objective, clientRequestId: input.clientRequestId, clientMessageId: input.clientMessageId, ...(input.sourceKind ? { sourceKind: input.sourceKind } : {}) }
      if (!validateGoalSetParams(params)) throw new GoalCenterError('invalid', 'goals.set params violated Contract')
      try {
        const raw = await transport.request<GoalSetWireResult>(GOALS_SET_METHOD, params, optionsFor(options?.signal))
        if (!validateGoalSetResult(raw)) throw new Error('goals.set returned an invalid response')
        if (raw.accepted !== true || !Object.prototype.hasOwnProperty.call(raw, 'goal')) {
          throw new Error('goals.set response is missing its acceptance outcome')
        }
        return {
          sessionKey: text(raw.sessionKey, raw.session_key), sessionId: text(raw.sessionId, raw.session_id), epoch: integer(raw.epoch),
          clientRequestId: text(raw.clientRequestId, raw.client_request_id), userMessageId: text(raw.userMessageId, raw.user_message_id) ?? null,
          previousGoalId: text(raw.previousGoalId, raw.previous_goal_id) ?? null, goal: snapshot(raw.goal),
          accepted: raw.accepted ?? undefined, replayed: raw.replayed ?? undefined, status: raw.status ?? undefined,
          taskId: text(raw.taskId, raw.task_id) ?? null, continuityToken: text(raw.continuityToken, raw.continuity_token),
        }
      } catch (error) { throw mapError(error) }
    },
  }
}
