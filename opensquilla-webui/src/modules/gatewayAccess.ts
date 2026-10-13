import type { InjectionKey } from 'vue'
import type { DesktopResumeSource } from '@/platform/types'

export type GatewayAvailability = 'unavailable' | 'preparing' | 'available'
export type GatewayConnectionHealth = 'healthy' | 'suspect'
export type GatewayConnectionPhase = 'healthy' | 'checking' | 'suspect' | 'reconnecting'
export type SupportBundleUnavailableReason = 'disconnected' | 'permission' | 'differentGateway' | null

export interface GatewayRunModePolicy {
  readonly allowedRunModes?: unknown
  readonly defaultRunMode?: unknown
  readonly fullHostAccessDisabledReason?: unknown
}

export interface GatewayConnectionSettings {
  readonly endpoint: string
  readonly credential?: string
}

/**
 * Application-facing projection of Gateway availability and caller scope.
 *
 * The v4 Adapter owns hello/auth parsing, capability method names, connection
 * storage and transport state. Vue consumers only see the product decisions
 * they need to render or guard a use case.
 */
export interface GatewayAccess {
  readonly availability: GatewayAvailability
  /** Transport health of the current connection; suspect is never user-visible as connected. */
  readonly connectionHealth: GatewayConnectionHealth
  readonly connectionPhase?: GatewayConnectionPhase
  /** Native resume is pending a liveness confirmation for the current socket. */
  readonly isResuming?: boolean
  readonly resumeSource?: DesktopResumeSource | null
  /** The local supervisor is preparing the runtime; no connection has failed. */
  readonly isRuntimeStarting: boolean
  readonly connectionError: string | null
  /** Host and port of the healthy connection, without credentials or URL suffixes. */
  readonly connectedGatewayHost?: string | null
  readonly requiresCredential: boolean
  readonly isAvailable: boolean
  readonly isLocalOwner: boolean
  /** A support bundle requires owner authority and the current HTTP target. */
  readonly supportBundleUnavailableReason: SupportBundleUnavailableReason
  readonly isAuthenticated: boolean
  /** Current anonymous session namespace, verified from this connection's Hello. */
  readonly guestSessionOwnerId: string | null
  /** Proven delivery authority; retained only while the same connection intent retries. */
  readonly deliveryIdentity: string | null
  readonly canManageProjectWorkspaces: boolean
  readonly canChooseProject: boolean
  readonly runModePolicy: GatewayRunModePolicy | null
  readonly streamIdleTimeoutMs: number | null
  readonly concurrentHistoryReads: boolean
  /** Gateway understands model/provider pins on the first atomic chat.send. */
  readonly chatSendInitialModel: boolean
  /** Gateway can atomically update model/provider and routing for an idle session. */
  readonly sessionsRoutingModelSelection: boolean
  readonly detachedSessionHydration: boolean
  readonly turnCommittedEvents: boolean
  readonly subscriptionEpoch: number
  loadConnectionEndpoint(): string
  connect(settings: GatewayConnectionSettings): Promise<void>
  disconnect(): void
  recoverSubscriptionEpoch(expectedEpoch: number, reason: string): boolean
}

export const GATEWAY_ACCESS_KEY: InjectionKey<GatewayAccess> = Symbol('GatewayAccess')
