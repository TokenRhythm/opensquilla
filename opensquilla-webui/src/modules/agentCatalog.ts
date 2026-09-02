import type { InjectionKey } from 'vue'
import type { Agent } from '@/types/agents'

export interface AgentMutationResult {
  readonly status?: string
  readonly agent?: Agent
  readonly [key: string]: unknown
}

export interface AgentCatalog {
  list(options?: { readonly signal?: AbortSignal }): Promise<readonly Agent[]>
  create(input: Readonly<Record<string, unknown>>, options?: {
    readonly signal?: AbortSignal
  }): Promise<AgentMutationResult>
  update(input: Readonly<Record<string, unknown>>, options?: {
    readonly signal?: AbortSignal
  }): Promise<AgentMutationResult>
  remove(agentId: string, options?: { readonly signal?: AbortSignal }): Promise<AgentMutationResult>
}

export const AGENT_CATALOG_KEY: InjectionKey<AgentCatalog> = Symbol('AgentCatalog')
