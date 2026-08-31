import type { InjectionKey } from 'vue'
import type {
  CollaborationMode,
  CollaborationSnapshot,
  PlanCardActionTarget,
  PlanRevisionRequest,
  PlanRevisionSnapshot,
  PlanRunSnapshot,
} from '@/types/plans'

export interface PlanRequestOptions { signal?: AbortSignal }
export interface PlanMutationResult {
  readonly accepted?: boolean
  readonly replayed?: boolean
  readonly sessionKey?: string
  readonly sessionId?: string
  readonly collaboration?: CollaborationSnapshot
  readonly currentPlan?: PlanRevisionSnapshot | null
  readonly planRevision?: PlanRevisionSnapshot | null
  readonly planRun?: PlanRunSnapshot | null
  readonly activePlanRun?: PlanRunSnapshot | null
  readonly [key: string]: unknown
}

export interface PlanEvent {
  readonly kind: 'collaboration' | 'revision' | 'run'
  readonly sessionKey?: string
  readonly collaboration?: CollaborationSnapshot
  readonly plan?: PlanRevisionSnapshot
  readonly run?: PlanRunSnapshot
}

export interface PlanCenter {
  available(operation?: 'mode' | 'mutations'): boolean
  setMode(sessionKey: string, mode: CollaborationMode, expectedRevision: number, options?: PlanRequestOptions): Promise<PlanMutationResult>
  revise(sessionKey: string, request: PlanRevisionRequest, clientRequestId: string, options?: PlanRequestOptions): Promise<PlanMutationResult>
  implement(sessionKey: string, target: PlanCardActionTarget, clientRequestId: string, options?: PlanRequestOptions & { intent?: string }): Promise<PlanMutationResult>
  cancelRun(sessionKey: string, runId: string, expectedStateRevision?: number, options?: PlanRequestOptions): Promise<PlanMutationResult>
  subscribe(listener: (event: PlanEvent) => void): { close(): void }
}

export const PLAN_CENTER_KEY: InjectionKey<PlanCenter> = Symbol('PlanCenter')
