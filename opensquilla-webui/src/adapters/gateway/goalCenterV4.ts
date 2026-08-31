import type { RpcCallOptions } from '@/lib/rpc'
import { GOALS_CAPABILITIES_METHOD, type Result as GoalCapabilitiesWireResult } from '@/contracts/generated/v4/goalsCapabilities'
import { validateResult as validateGoalCapabilitiesResult } from '@/contracts/generated/v4/goalsCapabilitiesValidators.mjs'
import { GOALS_SET_METHOD, type Params as GoalSetParams, type Result as GoalSetWireResult } from '@/contracts/generated/v4/goalsSet'
import { validateParams as validateGoalSetParams, validateResult as validateGoalSetResult } from '@/contracts/generated/v4/goalsSetValidators.mjs'
import { GOALS_STATUS_METHOD, type Params as GoalStatusParams, type Result as GoalStatusWireResult } from '@/contracts/generated/v4/goalsStatus'
import { validateParams as validateGoalStatusParams, validateResult as validateGoalStatusResult } from '@/contracts/generated/v4/goalsStatusValidators.mjs'
import type { GoalCapabilities, GoalCenter, GoalSetResult, GoalStatusResult } from '@/modules/goalCenter'
import { GoalCenterError } from '@/modules/goalCenter'
import { projectGoalSnapshot } from './goalSnapshotProjection'
import { mapGoalError } from './goalErrorMapping'

interface GoalCenterTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports?(method: string): boolean
}

const text = (...values: unknown[]): string | undefined => values.find(value => typeof value === 'string' && value.trim()) as string | undefined
const integer = (...values: unknown[]): number | undefined => values.find(value => typeof value === 'number' && Number.isInteger(value) && value >= 0) as number | undefined

function optionsFor(signal: AbortSignal | undefined): RpcCallOptions | undefined { return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined }

export function createV4GoalCenter(transport: GoalCenterTransport): GoalCenter {
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
  }
}
