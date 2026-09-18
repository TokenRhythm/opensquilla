import type { InjectionKey } from 'vue'

export type GoalStatus = string
export type GoalUsageCoverage = 'complete' | 'partial_history' | 'partial_usage'

/** Missing/future coverage never implies that budget accounting is trustworthy. */
export function normalizeGoalUsageCoverage(value: unknown): GoalUsageCoverage | undefined {
  return value === 'complete' || value === 'partial_history' || value === 'partial_usage'
    ? value : undefined
}

export function goalUsageSupportsBudget(value: unknown): boolean {
  return value === 'complete' || value === 'partial_history'
}

/** Domain projection of a durable goal; wire aliases stay in the adapter. */
export interface GoalExecutionOptions {
  readonly tokenBudget?: number | null
  readonly executionPolicy?: 'foreground' | 'background'
}

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
  readonly tokenBudget?: number | null
  readonly budgetTokensUsed?: number
  readonly usageCoverage?: 'complete' | 'partial_history' | 'partial_usage'
  readonly usageAccountingStartedAtMs?: number | null
  readonly executionPolicy?: 'foreground' | 'background'
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

export interface GoalSetInput extends GoalExecutionOptions {
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
  readonly tokenBudgetSupported: boolean
  readonly backgroundExecutionSupported: boolean
}

/** Omit unsupported settings while preserving ordinary Goal commands. */
export function supportedGoalExecutionOptions(
  options: GoalExecutionOptions,
  capabilities: Pick<GoalCapabilities, 'tokenBudgetSupported' | 'backgroundExecutionSupported'>,
): GoalExecutionOptions {
  return {
    ...(capabilities.tokenBudgetSupported && options.tokenBudget !== undefined
      ? { tokenBudget: options.tokenBudget } : {}),
    ...(capabilities.backgroundExecutionSupported && options.executionPolicy !== undefined
      ? { executionPolicy: options.executionPolicy } : {}),
  }
}

export type GoalCenterErrorCode = 'not-found' | 'unsupported' | 'forbidden' | 'conflict' | 'unavailable' | 'invalid'

export type GoalCenterFailureReason =
  | 'invalid-objective'
  | 'invalid-command'
  | 'not-found'
  | 'session-changed'
  | 'changed'
  | 'already-active'
  | 'busy'
  | 'not-resumable'
  | 'execution-disabled'
  | 'connection-required'
  | 'plan-mode-active'
  | 'plan-run-active'
  | 'request-conflict'

export class GoalCenterError extends Error {
  readonly code: GoalCenterErrorCode
  readonly reason?: GoalCenterFailureReason
  readonly retryable?: boolean
  readonly details?: unknown

  constructor(code: GoalCenterErrorCode, message: string, options: {
    reason?: GoalCenterFailureReason
    retryable?: boolean
    details?: unknown
    cause?: unknown
  } = {}) {
    super(message)
    this.name = 'GoalCenterError'
    this.code = code
    this.reason = options.reason
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
  edit(input: GoalMutationInput & GoalExecutionOptions & { objective: string }, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  pause(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  resume(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
  clear(input: GoalMutationInput, options?: { signal?: AbortSignal }): Promise<GoalMutationResult>
}

export const GOAL_CENTER_KEY: InjectionKey<GoalCenter> = Symbol('GoalCenter')
