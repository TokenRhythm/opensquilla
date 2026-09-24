import {
  readTransportFailure,
} from './transportTypes'
import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  AGENTS_LIST_METHOD,
  type Params as AgentsListParams,
  type Result as AgentsListResult,
} from '@/contracts/generated/v4/agentsList'
import { validateResult as validateAgentsListResult } from '@/contracts/generated/v4/agentsListValidators.mjs'
import type { AgentCatalog } from '@/modules/agentCatalog'
import { AgentCatalogError } from '@/modules/agentCatalog'
import type { Agent } from '@/types/agents'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { signal?: AbortSignal }): Promise<void>
}

const callOptions = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

function invalid(method: string): Error {
  return new AgentCatalogError('invalid', `${method} returned an invalid response`)
}

function mapAgentCatalogError(error: unknown): AgentCatalogError {
  if (error instanceof AgentCatalogError) return error
  const failure = readTransportFailure(error)
  const code = failure.code?.trim().toLowerCase().replace(/_/g, '.') || ''
  const kind = code === 'forbidden' || code === 'unauthorized'
    ? 'forbidden'
    : code === 'invalid.params' || code === 'invalid.request'
      ? 'invalid'
      : 'unavailable'
  return new AgentCatalogError(kind, failure.message, error)
}

async function requestAgentCatalog<T>(
  rpc: RpcTransport,
  method: string,
  params: Record<string, unknown>,
  options: RpcCallOptions,
): Promise<T> {
  try {
    return await rpc.request<T>(method, params, options)
  } catch (error) {
    throw mapAgentCatalogError(error)
  }
}

export function createV4AgentCatalog(rpc: RpcTransport): AgentCatalog {
  return {
    async list(options) {
      try {
        await rpc.ready({ signal: options?.signal })
      } catch (error) {
        throw mapAgentCatalogError(error)
      }
      const params: AgentsListParams = {}
      const result = await requestAgentCatalog<AgentsListResult>(
        rpc,
        AGENTS_LIST_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateAgentsListResult(result)) throw invalid(AGENTS_LIST_METHOD)
      return result.agents as Agent[]
    },
  }
}
