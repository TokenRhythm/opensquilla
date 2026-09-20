import type { InjectionKey } from 'vue'
import type { GatewayModelRoutingMode } from '@/types/modelRouting'

export interface SessionModelSelection {
  model: string
  provider: string
}

/** Legacy sessions can have an explicit model without a pinned provider. */
export interface StoredSessionModelSelection {
  model: string
  provider: string | null
}

export interface SessionRoutingSnapshot {
  key: string
  mode: GatewayModelRoutingMode
  revision: number
  source: string
  initialized: boolean
  appliesTo: string
  modelSelection?: StoredSessionModelSelection | null
}

export interface SessionRoutingSetInput {
  sessionKey: string
  mode: GatewayModelRoutingMode
  expectedRevision: number
  /** Omitted preserves the current pin; null returns this session to defaults. */
  modelSelection?: SessionModelSelection | null
}

export interface SessionRoutingSubscription {
  close(): void
}

export type SessionRoutingErrorCode =
  | 'not-found'
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'
  | 'busy'

export class SessionRoutingError extends Error {
  readonly code: SessionRoutingErrorCode
  readonly details?: unknown
  readonly retryable?: boolean

  constructor(code: SessionRoutingErrorCode, message: string, options: { details?: unknown; retryable?: boolean; cause?: unknown } = {}) {
    super(message)
    if (options.cause !== undefined) (this as Error & { cause?: unknown }).cause = options.cause
    this.name = 'SessionRoutingError'
    this.code = code
    this.details = options.details
    this.retryable = options.retryable
  }
}

export interface SessionRouting {
  /** Whether this Gateway advertises both session-routing operations. */
  available(): boolean
  get(sessionKey: string, options?: { signal?: AbortSignal }): Promise<SessionRoutingSnapshot>
  set(input: SessionRoutingSetInput, options?: { signal?: AbortSignal }): Promise<SessionRoutingSnapshot>
  subscribe(listener: (snapshot: SessionRoutingSnapshot) => void): SessionRoutingSubscription
  dispose(): void
}

export const SESSION_ROUTING_KEY: InjectionKey<SessionRouting> = Symbol('SessionRouting')
