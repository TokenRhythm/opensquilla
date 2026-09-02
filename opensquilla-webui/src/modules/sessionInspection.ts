import type { InjectionKey } from 'vue'
import type {
  SessionReadHistoryOptions,
  SessionReadHistoryPage,
} from './sessionReadLifecycle'

export interface SessionInspectionPreview {
  readonly key: string
  readonly title: string
  readonly lastMessage: string
  readonly updatedAt: number | null
}

export interface SessionInspectionRequestOptions {
  readonly signal?: AbortSignal
  readonly budgetMs?: number
  readonly deadlineAt?: number
}

export interface SessionInspectionHistory {
  latest(
    sessionKey: string,
    options?: SessionReadHistoryOptions,
  ): Promise<SessionReadHistoryPage>
  before(
    sessionKey: string,
    cursor: string,
    options?: SessionReadHistoryOptions,
  ): Promise<SessionReadHistoryPage>
}

/** Read-only inspection seam. It never subscribes or creates a live lease. */
export interface SessionInspection {
  preview(
    sessionKey: string,
    options?: SessionInspectionRequestOptions,
  ): Promise<SessionInspectionPreview | null>
  readonly history: SessionInspectionHistory
}

export class SessionInspectionContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SessionInspectionContractError'
  }
}

export const SESSION_INSPECTION_KEY: InjectionKey<SessionInspection> = Symbol('SessionInspection')
