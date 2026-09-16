import type { InjectionKey } from 'vue'

export type ApprovalNamespace = 'exec' | 'plugin'
export type ApprovalMode = 'prompt' | 'auto-approve' | 'auto-deny'
export type ApprovalDecision = 'allow-once' | 'allow-always' | 'deny'

/**
 * Semantic availability exposed by the ApprovalCenter seam.
 *
 * The Gateway adapter maps its private WebSocket states onto this closed
 * union.  Domain consumers therefore do not need to import (or compare)
 * transport-specific strings such as `_state`/`connected`.
 */
export type ApprovalAvailability = 'available' | 'recovering' | 'unavailable'

export type ApprovalErrorKind =
  | 'not-found'
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'

/** Transport-independent error exposed by the ApprovalCenter seam. */
export class ApprovalCenterError extends Error {
  readonly name = 'ApprovalCenterError'

  constructor(
    readonly kind: ApprovalErrorKind,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
  }
}

export interface ApprovalItem {
  id: string
  namespace: ApprovalNamespace
  toolName: string
  command: string
  approvalKind: string
  args: Record<string, unknown> | null
  warning: string
  displayKind?: string
  displayTarget?: string
  destructive?: boolean
  irreversible?: boolean
  backupState?: string
  agent: string
  sessionKey: string
  deadline: number
}

export interface ApprovalSnapshot {
  pending: readonly ApprovalItem[]
  mode: ApprovalMode
}

export interface ApprovalStatus {
  id: string
  namespace: ApprovalNamespace
  found?: boolean
  pending: boolean
  resolutionInProgress: boolean
  resolved: boolean
  approved: boolean
  resolution: string
  consumed: boolean
  deadline: number | null
}

export interface ApprovalResolveResult {
  approved?: boolean
  resolved?: boolean
  pending?: boolean
  resolution?: string
  resolutionInProgress?: boolean
}

export type ApprovalEventKind = 'requested' | 'updated' | 'resolved'

/** A wire-independent approval lifecycle notice. */
export interface ApprovalEvent {
  kind: ApprovalEventKind
  approvalId: string
  namespace: ApprovalNamespace
  approval?: ApprovalItem
  sessionKey: string | null
  approved: boolean | null
  resolution: string | null
  emittedAt: number | null
  activityOrder?: number
  /** True when an old lean event omitted display fields and needs hydration. */
  needsHydration: boolean
}

export interface ApprovalRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ApprovalSubscription {
  close(): void
}

export interface ApprovalCenter {
  setElevatedMode(
    sessionKey: string,
    mode: 'off' | 'on' | 'bypass' | 'full',
    options?: ApprovalRequestOptions,
  ): Promise<void>
  snapshot(options?: ApprovalRequestOptions): Promise<ApprovalSnapshot>
  status(
    namespace: ApprovalNamespace,
    id: string,
    options?: ApprovalRequestOptions,
  ): Promise<ApprovalStatus>
  resolve(
    request: {
      namespace: ApprovalNamespace
      id: string
      decision: ApprovalDecision
    },
    options?: ApprovalRequestOptions,
  ): Promise<ApprovalResolveResult>
  extend(
    namespace: ApprovalNamespace,
    id: string,
    seconds?: number,
    options?: ApprovalRequestOptions,
  ): Promise<ApprovalStatus>
  subscribe(listener: (event: ApprovalEvent) => void): ApprovalSubscription
  subscribeAvailability(listener: (state: ApprovalAvailability) => void): ApprovalSubscription
  dispose(): void
}

export const APPROVAL_CENTER_KEY: InjectionKey<ApprovalCenter> = Symbol('ApprovalCenter')

export function approvalChoiceForDecision(decision: ApprovalDecision): string {
  if (decision === 'allow-once') return 'allow_once'
  if (decision === 'allow-always') return 'allow_same_type'
  return 'deny'
}
