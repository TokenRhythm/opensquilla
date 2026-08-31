import type { InjectionKey } from 'vue'

export interface SetupCatalog {
  readonly providers?: readonly Record<string, unknown>[]
  readonly channels?: readonly Record<string, unknown>[]
  readonly searchProviders?: readonly Record<string, unknown>[]
  readonly routerProfiles?: readonly Record<string, unknown>[]
  readonly memoryEmbeddingProviders?: readonly Record<string, unknown>[]
  readonly imageGenerationProviders?: readonly Record<string, unknown>[]
  readonly audioProviders?: readonly Record<string, unknown>[]
  readonly [key: string]: unknown
}

export interface SetupStatus {
  readonly [key: string]: unknown
}

export interface SetupDiscoveryResult extends SetupStatus {
  readonly ok?: boolean
  readonly providerId?: string
  readonly failureKind?: string
  readonly message?: string
  readonly detail?: string
  readonly source?: string
  readonly models?: unknown
  readonly catalog?: unknown
  readonly firstResponseMs?: number
  readonly totalMs?: number
  readonly latencyMs?: number
}

export type SetupPayload = Record<string, unknown>

export interface SetupCatalogPort {
  catalog(options?: { signal?: AbortSignal }): Promise<SetupCatalog>
  discoverImageGenerationModels(providerId: string, options?: { signal?: AbortSignal }): Promise<SetupDiscoveryResult>
}

export interface SetupStatusPort {
  status(options?: { signal?: AbortSignal }): Promise<SetupStatus>
}

export interface ProviderSetup {
  configure(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  probe(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  discoverModels(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupDiscoveryResult>
  credentialReveal(providerId: string, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  credentialClear(providerId: string, options?: { signal?: AbortSignal }): Promise<SetupStatus>
}

export interface ProfileLifecycle {
  upsert(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  activate(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  probe(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  probeDraft(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupDiscoveryResult>
  discoverModels(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupDiscoveryResult>
  discoverDraftModels(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupDiscoveryResult>
  remove(providerId: string, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  removeActive(payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  credentialClear(providerId: string, options?: { signal?: AbortSignal }): Promise<SetupStatus>
}

export interface CapabilitySetup {
  configure(capability: string, payload: SetupPayload, options?: { signal?: AbortSignal }): Promise<SetupStatus>
  reset(capability: string, options?: { signal?: AbortSignal }): Promise<SetupStatus>
}

export interface SetupCapabilities {
  readonly profileLifecycle: boolean
  readonly primaryProviderRemoval: boolean
  readonly imageModelDiscovery: boolean
}

export interface SetupWorkflow extends SetupCatalogPort, SetupStatusPort {
  readonly capabilities: SetupCapabilities
  readonly provider: ProviderSetup
  readonly profile: ProfileLifecycle
  readonly capability: CapabilitySetup
}

export const SETUP_WORKFLOW_KEY: InjectionKey<SetupWorkflow> = Symbol('SetupWorkflow')
