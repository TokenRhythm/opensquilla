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

export interface SetupStatus { readonly [key: string]: unknown }

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

export interface SetupRequestOptions { signal?: AbortSignal }

export interface ConfigurePrimaryProvider {
  providerId: string
  model?: string | null
  apiKey?: string | null
  apiKeyEnv?: string | null
  preserveApiKey?: boolean | null
  baseUrl?: string | null
  proxy?: string | null
  presetId?: string | null
  routerAction?: string | null
  imageGenerationIntent?: string | null
}
export interface ProbePrimaryProvider extends ConfigurePrimaryProvider {}
export interface DiscoverPrimaryModels extends Omit<ConfigurePrimaryProvider, 'model' | 'presetId' | 'routerAction' | 'imageGenerationIntent'> { forceRefresh?: boolean | null }

export interface UpsertProfile {
  providerId: string
  model?: string | null
  apiKey?: string | null
  apiKeyEnv?: string | null
  apiKeyEnvPool?: readonly string[] | null
  keepCurrentSecret?: boolean | null
  preserveApiKey?: boolean | null
  baseUrl?: string | null
  proxy?: string | null
}
export interface ActivateProfile {
  providerId: string
  model?: string | null
  routerAction?: string | null
  imageGenerationIntent?: string | null
}
export interface RemoveActiveProfile {
  providerId: string
  replacementProviderId: string
  replacementModel?: string | null
  routerAction?: string | null
  imageGenerationIntent?: string | null
}
export interface ProfileProbe {
  providerId: string
  model?: string | null
  apiKey?: string | null
  apiKeyEnv?: string | null
  keepCurrentSecret?: boolean | null
  baseUrl?: string | null
  proxy?: string | null
  forceRefresh?: boolean | null
}

export interface ConfigureRouter {
  mode?: string | null
  defaultTier?: string | null
  tiers?: Readonly<Record<string, unknown>> | null
  crossProviderTiers?: boolean | null
  tierProviderMismatch?: string | null
}
export interface ConfigureEnsemble {
  enabled?: boolean | null
  selectionMode?: string | null
  modelOptions?: readonly unknown[] | null
  candidates?: readonly unknown[] | null
  minSuccessfulProposers?: number | null
  proposerMaxRetries?: number | null
  allFailedPolicy?: string | null
}
export interface ConfigureSearch {
  providerId: string
  apiKey?: string | null
  apiKeyEnv?: string | null
  maxResults?: number | string | null
  proxy?: string | null
  useEnvProxy?: boolean | null
  fallbackPolicy?: string | null
  diagnostics?: boolean | null
}
export interface ConfigureImageGeneration {
  providerId: string
  primary?: string | null
  apiKey?: string | null
  apiKeyEnv?: string | null
  baseUrl?: string | null
  enabled?: boolean | null
  size?: string | null
  outputFormat?: string | null
  fallbacks?: readonly unknown[] | null
  clearFallbacks?: boolean | null
  credentialMode?: string | null
}
export interface ConfigureMemoryEmbedding {
  providerId: string
  model?: string | null
  apiKey?: string | null
  apiKeyEnv?: string | null
  baseUrl?: string | null
  onnxDir?: string | null
}
export interface ConfigureAudio {
  providerId: string
  apiKey?: string | null
  apiKeyEnv?: string | null
  baseUrl?: string | null
  enabled?: boolean | null
  ttsVoice?: string | null
  ttsModel?: string | null
  languageCode?: string | null
}
export type ResettableCapability = 'search' | 'image_generation' | 'audio' | 'memory_embedding'

export interface SetupCatalogPort {
  catalog(options?: SetupRequestOptions): Promise<SetupCatalog>
  discoverImageGenerationModels(providerId: string, options?: SetupRequestOptions): Promise<SetupDiscoveryResult>
}
export interface SetupStatusPort { status(options?: SetupRequestOptions): Promise<SetupStatus> }

export interface ProviderSetup {
  configurePrimary(command: ConfigurePrimaryProvider, options?: SetupRequestOptions): Promise<SetupStatus>
  probePrimary(command: ProbePrimaryProvider, options?: SetupRequestOptions): Promise<SetupStatus>
  discoverPrimaryModels(command: DiscoverPrimaryModels, options?: SetupRequestOptions): Promise<SetupDiscoveryResult>
  revealActiveCredential(providerId: string, options?: SetupRequestOptions): Promise<SetupStatus>
  clearActiveCredential(providerId: string, options?: SetupRequestOptions): Promise<SetupStatus>
}
export interface ProfileLifecycle {
  upsertProfile(command: UpsertProfile, options?: SetupRequestOptions): Promise<SetupStatus>
  activateProfile(command: ActivateProfile, options?: SetupRequestOptions): Promise<SetupStatus>
  probeProfile(command: ProfileProbe, options?: SetupRequestOptions): Promise<SetupStatus>
  probeDraftProfile(command: ProfileProbe, options?: SetupRequestOptions): Promise<SetupDiscoveryResult>
  discoverProfileModels(command: ProfileProbe, options?: SetupRequestOptions): Promise<SetupDiscoveryResult>
  discoverDraftProfileModels(command: ProfileProbe, options?: SetupRequestOptions): Promise<SetupDiscoveryResult>
  removeProfile(providerId: string, options?: SetupRequestOptions): Promise<SetupStatus>
  removeActiveProfile(command: RemoveActiveProfile, options?: SetupRequestOptions): Promise<SetupStatus>
  clearProfileCredential(providerId: string, options?: SetupRequestOptions): Promise<SetupStatus>
}
export interface CapabilitySetup {
  configureRouter(command: ConfigureRouter, options?: SetupRequestOptions): Promise<SetupStatus>
  configureEnsemble(command: ConfigureEnsemble, options?: SetupRequestOptions): Promise<SetupStatus>
  configureSearch(command: ConfigureSearch, options?: SetupRequestOptions): Promise<SetupStatus>
  configureImageGeneration(command: ConfigureImageGeneration, options?: SetupRequestOptions): Promise<SetupStatus>
  configureMemoryEmbedding(command: ConfigureMemoryEmbedding, options?: SetupRequestOptions): Promise<SetupStatus>
  configureAudio(command: ConfigureAudio, options?: SetupRequestOptions): Promise<SetupStatus>
  resetCapability(capability: ResettableCapability, options?: SetupRequestOptions): Promise<SetupStatus>
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
