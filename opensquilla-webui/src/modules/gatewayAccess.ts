import type { InjectionKey } from 'vue'

export type GatewayAvailability = 'unavailable' | 'preparing' | 'available'

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
  readonly connectionError: string | null
  readonly isAvailable: boolean
  readonly isLocalOwner: boolean
  readonly isAuthenticated: boolean
  readonly canManageProjectWorkspaces: boolean
  readonly canChooseProject: boolean
  readonly runModePolicy: GatewayRunModePolicy | null
  readonly streamIdleTimeoutMs: number | null
  readonly concurrentHistoryReads: boolean
  readonly detachedSessionHydration: boolean
  readonly noninteractiveReceiptReplay?: boolean
  readonly subscriptionEpoch: number
  loadConnectionEndpoint(): string
  connect(settings: GatewayConnectionSettings): Promise<void>
  disconnect(): void
  recoverSubscriptionEpoch(expectedEpoch: number, reason: string): boolean
}

export const GATEWAY_ACCESS_KEY: InjectionKey<GatewayAccess> = Symbol('GatewayAccess')
