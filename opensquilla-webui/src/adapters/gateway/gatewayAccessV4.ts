import type {
  GatewayAccess,
  GatewayAvailability,
  GatewayConnectionSettings,
  GatewayRunModePolicy,
} from '@/modules/gatewayAccess'

const WS_URL_KEY = 'opensquilla.wsUrl'

interface GatewayAccessSource {
  readonly state: 'disconnected' | 'connecting' | 'connected'
  readonly error: string | null
  readonly isLocalOwner: boolean
  readonly canManageProjectWorkspaces: boolean
  readonly canChooseProject: boolean
  readonly auth: Record<string, unknown> | null
  readonly policy: Record<string, unknown> | null
  readonly connectionGeneration: number
  connect(url: string, token?: string): Promise<void>
  disconnect(): void
  recoverConnectionGeneration(expectedGeneration: number, reason: string): boolean
}

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
      return availability(source.state)
    },
    get connectionError() {
      return source.error
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
    get concurrentHistoryReads() {
      return source.policy?.concurrent_history_reads === true
    },
    get detachedSessionHydration() {
      const methods = source.policy?.concurrent_optional_read_methods
      return Array.isArray(methods) && methods.includes('sessions.messages.hydrate')
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
