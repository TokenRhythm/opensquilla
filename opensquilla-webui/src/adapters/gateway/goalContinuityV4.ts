import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import {
  GOALS_REATTACH_METHOD,
  type Params as GoalReattachParams,
  type Result as GoalReattachWireResult,
} from '@/contracts/generated/v4/goalsReattach'
import {
  validateParams as validateGoalReattachParams,
  validateResult as validateGoalReattachResult,
} from '@/contracts/generated/v4/goalsReattachValidators.mjs'
import { decodeConversationEvent } from './conversationEventsV4'
import { projectGoalSnapshot } from './goalSnapshotProjection'
import { mapGoalError } from './goalErrorMapping'
import type {
  GoalContinuity,
  GoalEvent,
  GoalEventSubscription,
  GoalReattachInput,
  GoalReattachResult,
} from '@/modules/goalContinuity'
import { GoalCenterError } from '@/modules/goalCenter'
import { canonicalSessionKey } from '@/utils/chat/sessionKeys'

const GOAL_EVENT = 'session.event.goal'

interface GoalContinuityRpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

interface GoalContinuityEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
}

type JsonObject = Record<string, unknown>

function textAlias(source: JsonObject, ...names: string[]): string | null {
  const values: string[] = []
  for (const name of names) {
    const value = source[name]
    if (value === undefined || value === null) continue
    if (typeof value !== 'string' || !value.trim()) {
      throw new Error(`${GOAL_EVENT} ${name} must be a non-empty string`)
    }
    values.push(value.trim())
  }
  const unique = new Set(values)
  if (unique.size > 1) {
    throw new Error(`${GOAL_EVENT} has conflicting aliases: ${names.join(', ')}`)
  }
  return values[0] ?? null
}

function integerAlias(source: JsonObject, ...names: string[]): number | null {
  const values: number[] = []
  for (const name of names) {
    const value = source[name]
    if (value === undefined || value === null) continue
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
      throw new Error(`${GOAL_EVENT} ${name} must be a non-negative integer`)
    }
    values.push(value)
  }
  const unique = new Set(values)
  if (unique.size > 1) {
    throw new Error(`${GOAL_EVENT} has conflicting numeric aliases: ${names.join(', ')}`)
  }
  return values[0] ?? null
}

function nullableTextAlias(source: JsonObject, ...names: string[]): string | null {
  const present = names.filter(name => Object.prototype.hasOwnProperty.call(source, name))
  const values: Array<string | null> = []
  for (const name of present) {
    const value = source[name]
    if (value === null) {
      values.push(null)
      continue
    }
    if (typeof value !== 'string') {
      throw new Error(`${GOAL_EVENT} ${name} must be a string or null`)
    }
    values.push(value)
  }
  const unique = new Set(values)
  if (unique.size > 1) {
    throw new Error(`${GOAL_EVENT} has conflicting nullable aliases: ${names.join(', ')}`)
  }
  return values[0] ?? null
}

function eventType(source: JsonObject): GoalEvent['eventType'] {
  const value = textAlias(source, 'eventType', 'event_type')
  if (value === null) return 'updated'
  if (value === 'created' || value === 'updated' || value === 'cleared') return value
  return 'unknown'
}

function projectEvent(payload: unknown, meta: unknown): GoalEvent | null {
  const decoded = decodeConversationEvent(GOAL_EVENT, payload, meta)
  if (!decoded.isKnown || !decoded.payload) return null
  const source = decoded.payload as JsonObject
  const nested = Object.prototype.hasOwnProperty.call(source, 'goal')
    ? source.goal
    : source
  const nestedSource = nested && typeof nested === 'object' && !Array.isArray(nested)
    ? nested as JsonObject
    : null
  const rawEnvelopeKey = decoded.sessionKey
    ?? textAlias(source, 'sessionKey', 'session_key', 'key')
    ?? null
  const envelopeKey = rawEnvelopeKey ? canonicalSessionKey(rawEnvelopeKey) : null
  const envelopeSessionId = textAlias(source, 'sessionId', 'session_id')
  const envelopeEpoch = integerAlias(source, 'epoch', 'sessionEpoch', 'session_epoch')
  // A nested Goal is an untrusted projection.  Never let a stale or malformed
  // nested identity cross the event boundary just because the outer payload
  // belongs to the current session.
  if (nestedSource) {
    const nestedKey = textAlias(nestedSource, 'sessionKey', 'session_key', 'key')
    const nestedSessionId = textAlias(nestedSource, 'sessionId', 'session_id')
    const nestedEpoch = integerAlias(nestedSource, 'epoch', 'sessionEpoch', 'session_epoch')
    if (
      (envelopeKey && nestedKey && !sameSessionKey(envelopeKey, nestedKey))
      || (envelopeSessionId && nestedSessionId && envelopeSessionId !== nestedSessionId)
      || (envelopeEpoch !== null && nestedEpoch !== null && envelopeEpoch !== nestedEpoch)
    ) {
      throw new Error(`${GOAL_EVENT} nested Goal crossed its session fence`)
    }
  }
  const goal = projectGoalSnapshot(nested)
  const key = envelopeKey
    ?? goal?.sessionKey
    ?? null
  const sessionId = envelopeSessionId ?? goal?.sessionId ?? null
  const epoch = envelopeEpoch ?? goal?.epoch ?? null
  const type = eventType(source)
  // A clear has no nested snapshot from which to recover identity.  Requiring
  // the complete event fence prevents an unrelated/malformed clear from
  // deleting the current Goal in a different session.
  if (!key || (type === 'cleared' && (sessionId === null || epoch === null))) {
    throw new Error(`${GOAL_EVENT} event is missing its session fence`)
  }
  return {
    eventType: type,
    sessionKey: key,
    sessionId,
    epoch,
    streamSeq: decoded.streamSeq ?? integerAlias(source, 'streamSeq', 'stream_seq'),
    streamGeneration: decoded.streamGeneration
      ?? textAlias(source, 'streamGeneration', 'stream_generation'),
    stateRevision: integerAlias(source, 'stateRevision', 'state_revision')
      ?? goal?.stateRevision
      ?? null,
    progressRevision: integerAlias(source, 'progressRevision', 'progress_revision')
      ?? goal?.progressRevision
      ?? null,
    objectiveRevision: integerAlias(source, 'objectiveRevision', 'objective_revision')
      ?? goal?.objectiveRevision
      ?? null,
    previousGoalId: nullableTextAlias(source, 'previousGoalId', 'previous_goal_id'),
    goal,
  }
}

function optionsFor(signal: AbortSignal | undefined): RpcCallOptions | undefined {
  return signal
    ? { signal, abortAction: 'reject', timeoutAction: 'reject' }
    : undefined
}

function normalizeReattachResult(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value
  const source = value as JsonObject
  const normalized = { ...source }
  const sessionKey = requiredTextAlias(source, 'sessionKey', 'session_key')
  const sessionId = requiredTextAlias(source, 'sessionId', 'session_id')
  const token = requiredTextAlias(source, 'continuityToken', 'continuity_token')
  const epoch = requiredIntegerAlias(source, 'epoch')
  if (sessionKey !== null) normalized.sessionKey = canonicalSessionKey(sessionKey)
  if (sessionId !== null) normalized.sessionId = sessionId
  if (token !== null) normalized.continuityToken = token
  if (epoch !== null) normalized.epoch = epoch
  return normalized
}

function requiredTextAlias(source: JsonObject, ...names: string[]): string | null {
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(source, name) && source[name] === null) {
      throw new Error(`${GOALS_REATTACH_METHOD} ${name} must not be null`)
    }
  }
  return textAlias(source, ...names)
}

function requiredIntegerAlias(source: JsonObject, ...names: string[]): number | null {
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(source, name) && source[name] === null) {
      throw new Error(`${GOALS_REATTACH_METHOD} ${name} must not be null`)
    }
  }
  return integerAlias(source, ...names)
}

function sameSessionKey(left: string, right: string): boolean {
  return left === right || canonicalSessionKey(left) === canonicalSessionKey(right)
}

function isCompleteGoalSnapshot(
  goal: GoalReattachResult['goal'],
): boolean {
  return Boolean(
    goal
    && typeof goal.goalId === 'string'
    && goal.goalId.length > 0
    && typeof goal.sessionKey === 'string'
    && goal.sessionKey.length > 0
    && typeof goal.sessionId === 'string'
    && goal.sessionId.length > 0
    && Number.isInteger(goal.epoch)
    && (goal.epoch ?? -1) >= 0
    && typeof goal.objective === 'string'
    && goal.objective.length > 0
    && typeof goal.status === 'string'
    && goal.status.length > 0
    && Number.isInteger(goal.stateRevision)
    && (goal.stateRevision ?? -1) >= 0
    && Number.isInteger(goal.objectiveRevision)
    && (goal.objectiveRevision ?? -1) >= 0
    && Number.isInteger(goal.progressRevision)
    && (goal.progressRevision ?? -1) >= 0
  )
}

export interface GoalContinuityAdapterOptions {
  warn?: (message: string, error?: unknown) => void
}

/** v4 adapter for Goal lease continuity and lifecycle events. */
export function createV4GoalContinuity(
  rpc: GoalContinuityRpcTransport,
  events?: GoalContinuityEventTransport,
  options: GoalContinuityAdapterOptions = {},
): GoalContinuity {
  const listeners = new Map<(event: GoalEvent) => void, number>()
  const warn = options.warn ?? ((message: string, error?: unknown) => {
    console.warn(`[GoalContinuity] ${message}`, error)
  })
  let disposed = false
  const upstream = events?.subscribe(GOAL_EVENT, (payload, meta) => {
    let event: GoalEvent | null
    try {
      event = projectEvent(payload, meta)
    } catch (error) {
      warn('Dropped malformed session.event.goal event', error)
      return
    }
    if (!event) return
    for (const listener of [...listeners.keys()]) {
      try {
        listener(event)
      } catch (error) {
        warn('Goal continuity listener failed', error)
      }
    }
  })

  return {
    async reattach(input: GoalReattachInput, options): Promise<GoalReattachResult> {
      const params: GoalReattachParams = {
        sessionKey: input.sessionKey,
        sessionId: input.sessionId,
        epoch: input.epoch,
        expectedGoalId: input.expectedGoalId,
        ...(input.continuityToken !== undefined
          ? { continuityToken: input.continuityToken }
          : {}),
        ...(input.takeover === true ? { takeover: true } : {}),
        ...(input.sourceKind ? { sourceKind: input.sourceKind } : {}),
      }
      if (!validateGoalReattachParams(params)) {
        throw new GoalCenterError('invalid', `${GOALS_REATTACH_METHOD} params violated Contract`)
      }
      try {
        const raw = await rpc.request<unknown>(
          GOALS_REATTACH_METHOD,
          params,
          optionsFor(options?.signal),
        )
        let normalized: unknown
        try {
          normalized = normalizeReattachResult(raw)
        } catch {
          throw new GoalCenterError('invalid', `${GOALS_REATTACH_METHOD} returned an invalid response`)
        }
        if (!validateGoalReattachResult(normalized)) {
          throw new GoalCenterError('invalid', `${GOALS_REATTACH_METHOD} returned an invalid response`)
        }
        const source = normalized as GoalReattachWireResult as unknown as JsonObject
        let sessionKey: string | null
        let sessionId: string | null
        let epoch: number | null
        let token: string | null
        let goal: GoalReattachResult['goal'] | null
        try {
          sessionKey = requiredTextAlias(source, 'sessionKey', 'session_key')
          sessionId = requiredTextAlias(source, 'sessionId', 'session_id')
          epoch = requiredIntegerAlias(source, 'epoch')
          token = requiredTextAlias(source, 'continuityToken', 'continuity_token')
          goal = projectGoalSnapshot(source.goal)
        } catch {
          throw new GoalCenterError('invalid', `${GOALS_REATTACH_METHOD} returned an invalid response`)
        }
        if (
          source.accepted !== true
          || !sessionKey
          || !sessionId
          || epoch === null
          || !token
          || !goal
          || !isCompleteGoalSnapshot(goal)
        ) {
          throw new GoalCenterError('invalid', `${GOALS_REATTACH_METHOD} response is missing its acceptance outcome`)
        }
        if (
          !sameSessionKey(sessionKey, input.sessionKey)
          || sessionId !== input.sessionId
          || epoch !== input.epoch
          || (goal.sessionKey !== undefined && !sameSessionKey(goal.sessionKey, sessionKey))
          || (goal.sessionId !== undefined && goal.sessionId !== sessionId)
          || (goal.epoch !== undefined && goal.epoch !== epoch)
          || (goal.goalId !== undefined && goal.goalId !== input.expectedGoalId)
        ) {
          throw new GoalCenterError(
            'conflict',
            `${GOALS_REATTACH_METHOD} response crossed the requested session fence`,
            { retryable: true },
          )
        }
        return { accepted: true, sessionKey, sessionId, epoch, goal, continuityToken: token }
      } catch (error) {
        throw mapGoalError(error, { reattach: true })
      }
    },
    subscribe(listener: (event: GoalEvent) => void): GoalEventSubscription {
      if (disposed) return { close() {} }
      listeners.set(listener, (listeners.get(listener) ?? 0) + 1)
      let active = true
      return {
        close() {
          if (!active) return
          active = false
          const count = listeners.get(listener) ?? 0
          if (count <= 1) listeners.delete(listener)
          else listeners.set(listener, count - 1)
        },
      }
    },
    dispose() {
      if (disposed) return
      disposed = true
      listeners.clear()
      upstream?.close()
    },
  }
}
