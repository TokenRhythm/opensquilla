import type { InjectionKey } from 'vue'

export interface ModelDescriptor {
  readonly id: string
  readonly name: string
  readonly provider: string
  readonly contextWindow: number
  readonly maxOutputTokens: number
  readonly capabilities: readonly string[]
  readonly pricing: { readonly inputPer1k: number; readonly outputPer1k: number }
  readonly source: string
  readonly reasoningFormat: string
  readonly metadata: Record<string, unknown> | null
}

export interface ProviderListError {
  readonly provider: string
  readonly kind: string
  readonly detail: string
}

export interface ModelCatalogResult {
  readonly models: readonly ModelDescriptor[]
  readonly errors: readonly ProviderListError[]
}

export interface ProviderDescriptor {
  readonly providerId: string
  readonly label?: string
  readonly backend?: string
  readonly providerKind?: string
  readonly runtimeSupported?: boolean
  readonly verification?: string
  readonly envKey?: string
  readonly defaultBaseUrl?: string
  readonly acceptsApiKey?: boolean
  readonly requiresApiKey?: boolean
  readonly requiresBaseUrl?: boolean
  readonly routerSupported?: boolean
  readonly deployment?: string
  readonly blocking?: boolean
  readonly canProbe?: boolean
  readonly whatYouNeed?: readonly string[]
  readonly defaultDirectModel?: string
  readonly defaultModel?: string
  readonly capabilities?: readonly string[]
  readonly fields?: readonly Record<string, unknown>[]
  readonly presets?: readonly Record<string, unknown>[]
  readonly [key: string]: unknown
}

export interface ProviderStatusRow {
  readonly providerId: string
  readonly active: boolean
  readonly configured: boolean
  readonly buildable: boolean
  readonly model: string | null
  readonly requiresApiKey: boolean
  readonly apiKeyEnv: string | null
  readonly apiKeyConfigured: boolean
  readonly apiKeyShape: string | null
  readonly baseUrlConfigured: boolean
  readonly error: string | null
  readonly modelProbe: ProviderModelProbe | null
  readonly latency: ProviderLatency | null
  readonly [key: string]: unknown
}

export interface ProviderModelProbe {
  readonly attempted: boolean
  readonly status: string
  readonly count: number
  readonly error: string | null
  readonly failureKind: string | null
}

export interface ProviderLatency {
  readonly p50TtftMs?: number | null
  readonly p95TtftMs?: number | null
  readonly samples?: number | null
  readonly windowMinutes?: number | null
}

export interface ProviderStatusResult {
  readonly activeProvider: string | null
  readonly providerResolution: Record<string, unknown>
  readonly providers: readonly ProviderStatusRow[]
  readonly count: number
  readonly [key: string]: unknown
}

export type RoutingMode = 'direct' | 'router' | 'ensemble'

export interface ModelRoutingSnapshot {
  readonly mode: RoutingMode
  readonly provider?: string
  readonly model?: string
  readonly revision?: number
  readonly [key: string]: unknown
}

export interface ProviderCredentials {
  reveal(providerId: string, options?: { signal?: AbortSignal }): Promise<Record<string, unknown>>
  clear(providerId: string, options?: { signal?: AbortSignal }): Promise<Record<string, unknown>>
}

export interface ProviderCatalog {
  catalog(options?: { signal?: AbortSignal }): Promise<readonly ProviderDescriptor[]>
}

export interface ModelCatalog {
  list(options?: { signal?: AbortSignal }): Promise<ModelCatalogResult>
}

export interface ProviderStatus {
  status(options?: ProviderStatusQuery): Promise<ProviderStatusResult>
}

export interface ProviderStatusQuery {
  readonly provider?: string
  readonly probeModels?: boolean
  readonly signal?: AbortSignal
}

export interface ModelRouting {
  get(options?: { signal?: AbortSignal }): Promise<ModelRoutingSnapshot>
  setRouting(mode: RoutingMode, options?: { signal?: AbortSignal }): Promise<ModelRoutingSnapshot>
  subscribeChanged(listener: (snapshot: ModelRoutingSnapshot) => void): { close(): void }
}

/** Composition object retained while pages migrate to narrow domain seams. */
export interface ProviderConfiguration extends ProviderCatalog, ModelCatalog, ProviderStatus, ModelRouting {
  credentials: ProviderCredentials
}

export const PROVIDER_CONFIGURATION_KEY: InjectionKey<ProviderConfiguration> = Symbol('ProviderConfiguration')
