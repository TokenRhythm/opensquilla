import type { InjectionKey } from 'vue'

export type GoalStatus = string

/** Domain projection of a durable goal; wire aliases stay in the adapter. */
export interface GoalSnapshot {
  readonly goalId?: string
  readonly sessionKey?: string
  readonly sessionId?: string
  readonly epoch?: number
  readonly objective?: string
  readonly status: GoalStatus
  readonly stateRevision?: number
  readonly objectiveRevision?: number
  readonly progressRevision?: number
  readonly progress?: unknown
  readonly continuationSeq?: number
  readonly activeTaskId?: string | null
  readonly executionState?: string
  readonly createdAt?: number
  readonly updatedAt?: number
  readonly finishedAt?: number | null
  readonly sourceMessageId?: string | null
  readonly terminalTurnId?: string | null
  readonly continuationDeferredReason?: string | null
  readonly turnsStarted?: number
  readonly turnsSettled?: number
  readonly windowTurnsStarted?: number
  readonly activeTimeMs?: number
  readonly windowActiveTimeMs?: number
  readonly usage?: unknown
  readonly pauseReason?: string | null
  readonly blockedReason?: string | null
  readonly terminalReason?: string | null
}

export interface GoalStatusResult {
  readonly sessionKey: string
  readonly sessionId: string
  readonly epoch: number
  readonly goal: GoalSnapshot | null
}

export interface GoalSetInput {
  readonly sessionKey: string
  readonly objective: string
  readonly clientRequestId: string
  readonly clientMessageId: string
  readonly sourceKind?: 'web' | 'cli'
}

export interface GoalSetResult {
  readonly accepted?: boolean
  readonly replayed?: boolean
  readonly clientRequestId?: string
  readonly sessionKey?: string
  readonly sessionId?: string
  readonly epoch?: number
  readonly taskId?: string | null
  readonly userMessageId?: string | null
  readonly previousGoalId?: string | null
  readonly goal?: GoalSnapshot | null
  readonly status?: string
  readonly continuityToken?: string
}

/** Common optimistic-concurrency input for Goal mutations. */
export interface GoalMutationInput {
  readonly sessionKey: string
  readonly expectedGoalId: string
  readonly expectedStateRevision: number
  readonly clientRequestId: string
  readonly sourceKind?: 'web' | 'cli'
}

export interface GoalMutationResult extends GoalSetResult {
  readonly accepted?: boolean
  readonly goal?: GoalSnapshot | null
}

/** Process-scoped Goal feature flags projected out of the v4 wire shape. */
export interface GoalCapabilities {
  readonly supported: boolean
  readonly executionEnabled: boolean
  readonly maxTurns: number
  readonly runtimeBudgetSeconds: number
  readonly methods: readonly string[]
}

export type GoalCenterErrorCode = 'not-found' | 'unsupported' | 'forbidden' | 'conflict' | 'unavailable' | 'invalid'

export class GoalCenterError extends Error {
  readonly code: GoalCenterErrorCode
  readonly retryable?: boolean
  readonly details?: unknown

  constructor(code: GoalCenterErrorCode, message: string, options: { retryable?: boolean; details?: unknown; cause?: unknown } = {}) {
    super(message)
    this.name = 'GoalCenterError'
    this.code = code
    this.retryable = options.retryable
    this.details = options.details
    if (options.cause !== undefined) (this as Error & { cause?: unknown }).cause = options.cause
  }
}

export interface GoalCenter {
  /** Report whether the requested Goal UX operation is available. */
  available(operation?: 'status' | 'set' | 'goal-mode'): boolean
  capabilities(options?: { signal?: AbortSignal }): Promise<GoalCapabilities>
  status(sessionKey: string, options?: { signal?: AbortSignal }): Promise<GoalStatusResult>
  set(input: GoalSetInput, options?: { signal?: AbortSignal }): Promise<GoalSetResult>
  edit(input: GoalMutationInput & { objective: string }, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  pause(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  resume(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  clear(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
}

export const GOAL_CENTER_KEY: InjectionKey<GoalCenter> = Symbol('GoalCenter')
