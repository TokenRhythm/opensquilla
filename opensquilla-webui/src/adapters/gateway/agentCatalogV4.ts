import type { RpcCallOptions } from '@/lib/rpc'
import type { AgentCatalog, AgentMutationResult } from '@/modules/agentCatalog'
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

const record = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

export function createV4AgentCatalog(rpc: RpcTransport): AgentCatalog {
  return {
    async list(options) {
      await rpc.ready({ signal: options?.signal })
      const result = record(await rpc.request(
        'agents.list',
        undefined,
        callOptions(options?.signal),
      ))
      return Array.isArray(result.agents)
        ? result.agents.filter(item => item && typeof item === 'object' && !Array.isArray(item)) as Agent[]
        : []
    },
    async create(input, options) {
      return record(await rpc.request(
        'agents.create',
        { ...input },
        callOptions(options?.signal),
      )) as AgentMutationResult
    },
    async update(input, options) {
      return record(await rpc.request(
        'agents.update',
        { ...input },
        callOptions(options?.signal),
      )) as AgentMutationResult
    },
    async remove(agentId, options) {
      return record(await rpc.request(
        'agents.delete',
        { id: agentId },
        callOptions(options?.signal),
      )) as AgentMutationResult
    },
  }
}
