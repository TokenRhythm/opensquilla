import type { InjectionKey } from 'vue'
import type { Agent } from '@/types/agents'

export interface AgentCatalogRequestOptions {
  readonly signal?: AbortSignal
}

export type AgentCatalogErrorKind =
  | 'already-exists'
  | 'not-found'
  | 'immutable'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'

/** Agent-management failure projected by its Gateway Adapter. */
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

export interface CreateAgentCommand {
  readonly id?: string
  readonly name?: string
  readonly description?: string
  readonly model?: string
  readonly workspace?: string
  readonly agentDir?: string
  readonly enabled?: boolean
  readonly systemPrompt?: string
  readonly tools?: readonly string[]
}

export interface UpdateAgentCommand {
  readonly id: string
  readonly name?: string
  readonly description?: string
  readonly model?: string
  readonly workspace?: string
  readonly agentDir?: string
  readonly enabled?: boolean
  readonly systemPrompt?: string
  readonly tools?: readonly string[]
}

export interface AgentCatalog {
  list(options?: AgentCatalogRequestOptions): Promise<readonly Agent[]>
  create(command: CreateAgentCommand, options?: AgentCatalogRequestOptions): Promise<Agent>
  update(command: UpdateAgentCommand, options?: AgentCatalogRequestOptions): Promise<Agent>
  remove(agentId: string, options?: AgentCatalogRequestOptions): Promise<void>
}

export const AGENT_CATALOG_KEY: InjectionKey<AgentCatalog> = Symbol('AgentCatalog')
