import type { InjectionKey } from 'vue'

export interface ProviderDescriptor {
  id: string
  label?: string
  models?: string[]
  configured?: boolean
  requiresApiKey?: boolean
  baseUrl?: string
  [key: string]: unknown
}

export interface ModelRoutingSnapshot {
  mode: string
  provider?: string
  model?: string
  revision?: number
  [key: string]: unknown
}

export interface ProviderConfiguration {
  list(options?: { signal?: AbortSignal }): Promise<ProviderDescriptor[]>
  status(options?: { signal?: AbortSignal }): Promise<Record<string, unknown>>
  getRouting(options?: { signal?: AbortSignal }): Promise<ModelRoutingSnapshot>
  setRouting(input: ModelRoutingSnapshot, options?: { signal?: AbortSignal }): Promise<ModelRoutingSnapshot>
}

export const PROVIDER_CONFIGURATION_KEY: InjectionKey<ProviderConfiguration> = Symbol('ProviderConfiguration')
