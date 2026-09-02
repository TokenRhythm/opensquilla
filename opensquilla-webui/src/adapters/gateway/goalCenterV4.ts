import type { RpcCallOptions } from '@/lib/rpc'
import { GOALS_CAPABILITIES_METHOD, type Result as GoalCapabilitiesWireResult } from '@/contracts/generated/v4/goalsCapabilities'
import { validateResult as validateGoalCapabilitiesResult } from '@/contracts/generated/v4/goalsCapabilitiesValidators.mjs'
import { GOALS_SET_METHOD, type Params as GoalSetParams, type Result as GoalSetWireResult } from '@/contracts/generated/v4/goalsSet'
import { validateParams as validateGoalSetParams, validateResult as validateGoalSetResult } from '@/contracts/generated/v4/goalsSetValidators.mjs'
import { GOALS_EDIT_METHOD } from '@/contracts/generated/v4/goalsEdit'
import { validateResult as validateGoalEditResult } from '@/contracts/generated/v4/goalsEditValidators.mjs'
import { GOALS_PAUSE_METHOD } from '@/contracts/generated/v4/goalsPause'
import { validateResult as validateGoalPauseResult } from '@/contracts/generated/v4/goalsPauseValidators.mjs'
import { GOALS_RESUME_METHOD } from '@/contracts/generated/v4/goalsResume'
import { validateResult as validateGoalResumeResult } from '@/contracts/generated/v4/goalsResumeValidators.mjs'
import { GOALS_CLEAR_METHOD } from '@/contracts/generated/v4/goalsClear'
import { validateResult as validateGoalClearResult } from '@/contracts/generated/v4/goalsClearValidators.mjs'
import { GOALS_STATUS_METHOD, type Params as GoalStatusParams, type Result as GoalStatusWireResult } from '@/contracts/generated/v4/goalsStatus'
import { validateParams as validateGoalStatusParams, validateResult as validateGoalStatusResult } from '@/contracts/generated/v4/goalsStatusValidators.mjs'
import type { GoalCapabilities, GoalCenter, GoalMutationInput, GoalMutationResult, GoalSetResult, GoalStatusResult } from '@/modules/goalCenter'
import { GoalCenterError } from '@/modules/goalCenter'
import { projectGoalSnapshot } from './goalSnapshotProjection'
import { mapGoalError } from './goalErrorMapping'

interface GoalCenterTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports?(method: string): boolean
}

const mutationValidators: Record<string, (value: unknown) => boolean> = {
  [GOALS_EDIT_METHOD]: validateGoalEditResult,
  [GOALS_PAUSE_METHOD]: validateGoalPauseResult,
  [GOALS_RESUME_METHOD]: validateGoalResumeResult,
  [GOALS_CLEAR_METHOD]: validateGoalClearResult,
}

const text = (...values: unknown[]): string | undefined => values.find(value => typeof value === 'string' && value.trim()) as string | undefined
const integer = (...values: unknown[]): number | undefined => values.find(value => typeof value === 'number' && Number.isInteger(value) && value >= 0) as number | undefined

function optionsFor(signal: AbortSignal | undefined): RpcCallOptions | undefined { return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined }

export function createV4GoalCenter(transport: GoalCenterTransport): GoalCenter {
    const mutate = async (
      method: 'goals.edit' | 'goals.pause' | 'goals.resume' | 'goals.clear',
      input: GoalMutationInput & { objective?: string },
      options?: { signal?: AbortSignal },
    ): Promise<GoalMutationResult> => {
      const params: Record<string, unknown> = {
        sessionKey: input.sessionKey,
        expectedGoalId: input.expectedGoalId,
        expectedStateRevision: input.expectedStateRevision,
        clientRequestId: input.clientRequestId,
        ...(input.sourceKind ? { sourceKind: input.sourceKind } : {}),
        ...(input.objective !== undefined ? { objective: input.objective } : {}),
      }
      try {
        const raw = await transport.request<Record<string, unknown>>(method, params, optionsFor(options?.signal))
        if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !mutationValidators[method](raw)) {
          throw new Error(`${method} returned an invalid response`)
        }
        const sessionKey = text(raw.sessionKey, raw.session_key)
        const sessionId = text(raw.sessionId, raw.session_id)
        const epoch = integer(raw.epoch)
        return {
          ...raw,
          sessionKey,
          sessionId,
          epoch,
          clientRequestId: text(raw.clientRequestId, raw.client_request_id) ?? input.clientRequestId,
          taskId: text(raw.taskId, raw.task_id) ?? null,
          userMessageId: text(raw.userMessageId, raw.user_message_id) ?? null,
          previousGoalId: text(raw.previousGoalId, raw.previous_goal_id) ?? null,
          goal: projectGoalSnapshot(raw.goal),
          accepted: raw.accepted === undefined ? undefined : raw.accepted === true,
          continuityToken: text(raw.continuityToken, raw.continuity_token),
        }
      } catch (error) { throw mapGoalError(error) }
    }
  return {
    available(operation = 'status') {
      if (!transport.supports) return true
      if (operation === 'goal-mode') {
        return transport.supports(GOALS_SET_METHOD) && transport.supports(GOALS_CAPABILITIES_METHOD)
      }
      return transport.supports(operation === 'set' ? GOALS_SET_METHOD : GOALS_STATUS_METHOD)
    },
    async capabilities(options): Promise<GoalCapabilities> {
      try {
        const raw = await transport.request<GoalCapabilitiesWireResult>(
          GOALS_CAPABILITIES_METHOD,
          undefined,
          optionsFor(options?.signal),
        )
        if (!validateGoalCapabilitiesResult(raw)) {
          throw new Error('goals.capabilities returned an invalid response')
        }
        return {
          supported: raw.supported,
          executionEnabled: raw.executionEnabled,
          maxTurns: raw.maxTurns,
          runtimeBudgetSeconds: raw.runtimeBudgetSeconds,
          methods: [...raw.methods],
        }
      } catch (error) { throw mapGoalError(error) }
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
        return { sessionKey: normalized.sessionKey, sessionId: normalized.sessionId, epoch: normalized.epoch, goal: projectGoalSnapshot(normalized.goal) }
      } catch (error) { throw mapGoalError(error) }
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
          previousGoalId: text(raw.previousGoalId, raw.previous_goal_id) ?? null, goal: projectGoalSnapshot(raw.goal),
          accepted: raw.accepted ?? undefined, replayed: raw.replayed ?? undefined, status: raw.status ?? undefined,
          taskId: text(raw.taskId, raw.task_id) ?? null, continuityToken: text(raw.continuityToken, raw.continuity_token),
        }
      } catch (error) { throw mapGoalError(error) }
    },
    edit(input, options) { return mutate('goals.edit', input, options) },
    pause(input, options) { return mutate('goals.pause', input, options) },
    resume(input, options) { return mutate('goals.resume', input, options) },
    clear(input, options) { return mutate('goals.clear', input, options) },
  }
}
