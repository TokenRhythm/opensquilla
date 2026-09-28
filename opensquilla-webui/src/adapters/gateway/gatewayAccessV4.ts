import type {
  GatewayAccess,
  GatewayAvailability,
  GatewayConnectionSettings,
  GatewayConnectionHealth,
  GatewayConnectionPhase,
  GatewayRunModePolicy,
} from '@/modules/gatewayAccess'
import { SESSIONS_MESSAGES_HYDRATE_METHOD } from '@/contracts/generated/v4/sessionsMessagesHydrate'
import {
  CONVERSATION_EVENT_WIRE_NAMES,
  conversationSemanticEventKind,
} from './conversationEventsV4'

const WS_URL_KEY = 'opensquilla.wsUrl'

interface GatewayAccessSource {
  readonly state: 'disconnected' | 'connecting' | 'connected'
  readonly health: GatewayConnectionHealth
  readonly phase?: GatewayConnectionPhase
  readonly isResuming?: boolean
  readonly resumeSource?: import('@/platform/types').DesktopResumeSource | null
  readonly runtimeStarting?: boolean
  readonly error: string | null
  readonly isLocalOwner: boolean
  readonly canManageProjectWorkspaces: boolean
  readonly canChooseProject: boolean
  readonly auth: Record<string, unknown> | null
  readonly policy: Record<string, unknown> | null
  readonly connectionGeneration: number
  readonly deliveryContext: {
    readonly targetId: string
    readonly principal: unknown
  } | null
  hasRpcEvent(event: string): boolean
  connect(url: string, token?: string): Promise<void>
  disconnect(): void
  recoverConnectionGeneration(expectedGeneration: number, reason: string): boolean
}

const TURN_COMMITTED_EVENT_NAMES = CONVERSATION_EVENT_WIRE_NAMES.filter(
  name => conversationSemanticEventKind(name) === 'turn-committed',
)

function defaultGatewayEndpoint(): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/ws`
}

function connectionEndpoint(): string {
  try {
    return localStorage.getItem(WS_URL_KEY) || defaultGatewayEndpoint()
  } catch {
    return defaultGatewayEndpoint()
  }
}

function availability(state: GatewayAccessSource['state']): GatewayAvailability {
  if (state === 'connected') return 'available'
  if (state === 'connecting') return 'preparing'
  return 'unavailable'
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function authenticated(auth: GatewayAccessSource['auth']): boolean {
  const principal = objectValue(auth?.principal)
  return principal?.authState === 'authenticated'
}

function guestSessionOwnerId(source: GatewayAccessSource): string | null {
  const principal = objectValue(source.auth?.principal)
  if (
    source.state !== 'connected'
    || source.isLocalOwner
    || principal?.isOwner !== false
    || principal.authenticated !== false
    || !['guest', 'invalid'].includes(String(principal.authState))
  ) return null
  const ownerId = principal.guestOwnerId
  return typeof ownerId === 'string' && /^[0-9a-f]{64}$/.test(ownerId) ? ownerId : null
}

function authoritySet(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.some(item => (
    typeof item !== 'string' || !item || item !== item.trim()
  ))) return null
  return [...new Set(value as string[])].sort()
}

function deliveryIdentity(source: GatewayAccessSource): string | null {
  const context = source.deliveryContext
  const principal = objectValue(context?.principal)
  if (!context?.targetId || !principal) return null
  const scopes = authoritySet(principal.scopes)
  const capabilities = authoritySet(principal.capabilities)
  const authState = principal.authState
  const tokenPublicId = principal.tokenPublicId ?? null
  const guestOwnerId = principal.guestOwnerId ?? null
  if (
    !['operator', 'node'].includes(String(principal.role))
    || typeof principal.authenticated !== 'boolean'
    || typeof principal.isOwner !== 'boolean'
    || !['authenticated', 'guest', 'invalid'].includes(String(authState))
    || !scopes || !capabilities
    || (tokenPublicId !== null && (
      typeof tokenPublicId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(tokenPublicId)
    ))
  ) return null
  if (authState === 'authenticated') {
    if ((!principal.authenticated && !principal.isOwner) || guestOwnerId !== null) return null
  } else if (
    principal.authenticated || principal.isOwner
    || typeof guestOwnerId !== 'string' || !/^[0-9a-f]{64}$/.test(guestOwnerId)
  ) return null
  // The opaque target id binds the actual endpoint/profile/credentials.
  // URLs and raw credentials never enter this serializable queue identity.
  return JSON.stringify([
    'delivery-v1', context.targetId, principal.role, authState,
    principal.authenticated, principal.isOwner, scopes, capabilities,
    tokenPublicId, guestOwnerId,
  ])
}

function runModePolicy(auth: GatewayAccessSource['auth']): GatewayRunModePolicy | null {
  const policy = objectValue(auth?.runModePolicy)
  if (!policy) return null
  return {
    allowedRunModes: policy.allowedRunModes,
    defaultRunMode: policy.defaultRunMode,
    fullHostAccessDisabledReason: policy.fullHostAccessDisabledReason,
  }
}

function streamIdleTimeoutMs(policy: GatewayAccessSource['policy']): number | null {
  const value = policy?.webui_stream_idle_grace_ms
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : null
}

/** Project v4 transport/hello state into semantic application capabilities. */
export function createV4GatewayAccess(source: GatewayAccessSource): GatewayAccess {
  return {
    get availability() {
      if (source.runtimeStarting && source.state !== 'connected') return 'preparing'
      return availability(source.state)
    },
    get connectionHealth() {
      return source.isResuming ? 'suspect' : source.health
    },
    get connectionPhase() {
      return source.phase || (source.isResuming ? 'checking' : source.health)
    },
    get isResuming() {
      return source.isResuming === true
    },
    get resumeSource() {
      return source.resumeSource ?? null
    },
    get isRuntimeStarting() {
      return source.runtimeStarting === true
    },
    get connectionError() {
      return source.error
    },
    get requiresCredential() {
      return source.error === 'authentication_failed' || source.error === 'authentication_mismatch'
    },
    get isAvailable() {
      return source.state === 'connected'
    },
    get isLocalOwner() {
      return source.isLocalOwner
    },
    get isAuthenticated() {
      return authenticated(source.auth)
    },
    get guestSessionOwnerId() {
      return guestSessionOwnerId(source)
    },
    get deliveryIdentity() {
      return deliveryIdentity(source)
    },
    get canManageProjectWorkspaces() {
      return source.canManageProjectWorkspaces
    },
    get canChooseProject() {
      return source.canChooseProject
    },
    get runModePolicy() {
      return runModePolicy(source.auth)
    },
    get streamIdleTimeoutMs() {
      return streamIdleTimeoutMs(source.policy)
    },
    get chatSendInitialModel() {
      return source.policy?.chat_send_initial_model === true
    },
    get sessionsRoutingModelSelection() {
      return source.policy?.sessions_routing_model_selection === true
    },
    get concurrentHistoryReads() {
      return source.policy?.concurrent_history_reads === true
    },
    get detachedSessionHydration() {
      const methods = source.policy?.concurrent_optional_read_methods
      return Array.isArray(methods) && methods.includes(SESSIONS_MESSAGES_HYDRATE_METHOD)
    },
    get turnCommittedEvents() {
      return TURN_COMMITTED_EVENT_NAMES.some(name => source.hasRpcEvent(name))
    },
    get subscriptionEpoch() {
      return source.connectionGeneration
    },
    loadConnectionEndpoint: connectionEndpoint,
    async connect(settings: GatewayConnectionSettings) {
      const endpoint = settings.endpoint.trim()
      await source.connect(endpoint || defaultGatewayEndpoint(), settings.credential?.trim() || undefined)
    },
    disconnect() {
      source.disconnect()
    },
    recoverSubscriptionEpoch(expectedEpoch, reason) {
      return source.recoverConnectionGeneration(expectedEpoch, reason)
    },
  }
}
