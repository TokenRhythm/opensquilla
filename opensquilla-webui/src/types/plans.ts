export interface PlanRevisionStep {
  stepId: string
  title: string
  details?: string
}

export interface PlanRevisionSnapshot {
  revisionId: string
  planId: string
  parentRevisionId?: string
  generation?: number
  title: string
  markdown: string
  steps: PlanRevisionStep[]
  current: boolean
  createdAt?: number
}

export type CollaborationMode = 'default' | 'plan'

export interface CollaborationSnapshot {
  mode: CollaborationMode
  revision: number
}

export type PlanRunStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'blocked'
  | 'completed'
  | 'cancelled'
  | 'superseded'

export type PlanRunStepStatus =
  | 'pending'
  | 'in_progress'
  | 'completed'
  | 'blocked'
  | 'skipped'

export interface PlanRunStepSnapshot {
  stepId: string
  title: string
  status: PlanRunStepStatus
  reason?: string
}

export interface PlanRunSnapshot {
  runId: string
  planRevisionId: string
  status: PlanRunStatus
  currentStepId?: string
  stateRevision?: number
  /** Execution owner. `manual` today; a future durable goal driver can reuse the same run. */
  driverKind?: string
  driverId?: string
  activeTaskId?: string
  pauseReason?: string
  terminalReason?: string
  createdAt?: number
  updatedAt?: number
  startedAt?: number
  finishedAt?: number
  steps: PlanRunStepSnapshot[]
}

export type PlanCardAction = 'implement-current' | 'implement-new' | 'replan'

export interface PlanCardActionTarget {
  planId: string
  revisionId: string
}

export interface PlanRevisionRequest extends PlanCardActionTarget {
  prompt: string
}
