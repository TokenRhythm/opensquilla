import type { InjectionKey } from 'vue'
import type { Agent } from '@/types/agents'

export interface AgentCatalogRequestOptions {
  readonly signal?: AbortSignal
}

export type AgentCatalogErrorKind =
  | 'forbidden'
  | 'unavailable'
  | 'invalid'

/** Agent-catalog read failure projected by its Gateway Adapter. */
export class AgentCatalogError extends Error {
  constructor(
    readonly kind: AgentCatalogErrorKind,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
    this.name = 'AgentCatalogError'
  }
}

/** Read-only runtime profiles for session metadata. */
export interface AgentCatalog {
  list(options?: AgentCatalogRequestOptions): Promise<readonly Agent[]>
}

export const AGENT_CATALOG_KEY: InjectionKey<AgentCatalog> = Symbol('AgentCatalog')
