import type { InjectionKey } from 'vue'
import type { GoalSnapshot } from './goalCenter'

/** The small, wire-independent event vocabulary consumed by Goal UI. */
export type GoalEventType = 'created' | 'updated' | 'cleared' | 'unknown'

export interface GoalEvent {
  readonly eventType: GoalEventType
  readonly sessionKey: string | null
  readonly sessionId: string | null
  readonly epoch: number | null
  readonly streamSeq: number | null
  readonly streamGeneration: string | null
  readonly stateRevision: number | null
  readonly progressRevision: number | null
  readonly objectiveRevision: number | null
  readonly previousGoalId: string | null
  readonly goal: GoalSnapshot | null
}

export interface GoalReattachInput {
  readonly sessionKey: string
  readonly sessionId: string
  readonly epoch: number
  readonly expectedGoalId: string
  readonly continuityToken?: string
  readonly takeover?: boolean
  readonly sourceKind?: 'web' | 'cli'
}

export interface GoalReattachResult {
  readonly accepted: true
  readonly sessionKey: string
  readonly sessionId: string
  readonly epoch: number
  readonly goal: GoalSnapshot
  readonly continuityToken: string
}

export interface GoalEventSubscription {
  close(): void
}

/** Domain seam for process-local Goal ownership continuity. */
export interface GoalContinuity {
  reattach(
    input: GoalReattachInput,
    options?: { signal?: AbortSignal },
  ): Promise<GoalReattachResult>
  subscribe(listener: (event: GoalEvent) => void): GoalEventSubscription
  dispose(): void
}

export const GOAL_CONTINUITY_KEY: InjectionKey<GoalContinuity> = Symbol('GoalContinuity')
