import type { InjectionKey } from 'vue'
import type {
  CollaborationMode,
  CollaborationSnapshot,
  PlanCardActionTarget,
  PlanPresentationSnapshot,
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
  readonly planPresentations?: PlanPresentationSnapshot[]
  readonly [key: string]: unknown
}

export interface PlanEvent {
  readonly kind: 'collaboration' | 'revision' | 'run' | 'presentation'
  readonly sessionKey?: string
  readonly epoch?: number
  readonly collaboration?: CollaborationSnapshot
  readonly plan?: PlanRevisionSnapshot
  readonly run?: PlanRunSnapshot
  readonly planPresentations?: PlanPresentationSnapshot[]
}

export interface PlanPresentationInput {
  sessionKey: string
  revisionId: string
  dismissed: boolean
  expectedEpoch: number
  expectedPresentationRevision: number
  clientRequestId: string
}

export interface PlanCenter {
  available(operation?: 'mode' | 'mutations' | 'presentation'): boolean
  setMode(sessionKey: string, mode: CollaborationMode, expectedRevision: number, options?: PlanRequestOptions): Promise<PlanMutationResult>
  revise(sessionKey: string, request: PlanRevisionRequest, clientRequestId: string, options?: PlanRequestOptions): Promise<PlanMutationResult>
  implement(sessionKey: string, target: PlanCardActionTarget, clientRequestId: string, options?: PlanRequestOptions & { intent?: string }): Promise<PlanMutationResult>
  cancelRun(sessionKey: string, runId: string, expectedStateRevision?: number, options?: PlanRequestOptions): Promise<PlanMutationResult>
  setPresentation(input: PlanPresentationInput, options?: PlanRequestOptions): Promise<PlanMutationResult>
  subscribe(listener: (event: PlanEvent) => void): { close(): void }
}

export const PLAN_CENTER_KEY: InjectionKey<PlanCenter> = Symbol('PlanCenter')
