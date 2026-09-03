import type { RpcCallOptions } from '@/lib/rpc'
import {
  AGENTS_LIST_METHOD,
  type Params as AgentsListParams,
  type Result as AgentsListResult,
} from '@/contracts/generated/v4/agentsList'
import { validateResult as validateAgentsListResult } from '@/contracts/generated/v4/agentsListValidators.mjs'
import {
  AGENTS_CREATE_METHOD,
  type Params as AgentsCreateParams,
  type Agent as AgentsCreateResult,
} from '@/contracts/generated/v4/agentsCreate'
import { validateAgent as validateAgentsCreateResult } from '@/contracts/generated/v4/agentsCreateValidators.mjs'
import {
  AGENTS_UPDATE_METHOD,
  type Params as AgentsUpdateParams,
  type Agent as AgentsUpdateResult,
} from '@/contracts/generated/v4/agentsUpdate'
import { validateAgent as validateAgentsUpdateResult } from '@/contracts/generated/v4/agentsUpdateValidators.mjs'
import {
  AGENTS_DELETE_METHOD,
  type Params as AgentsDeleteParams,
  type Result as AgentsDeleteResult,
} from '@/contracts/generated/v4/agentsDelete'
import { validateResult as validateAgentsDeleteResult } from '@/contracts/generated/v4/agentsDeleteValidators.mjs'
import type {
  AgentCatalog,
  CreateAgentCommand,
  UpdateAgentCommand,
} from '@/modules/agentCatalog'
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

function createParams(command: CreateAgentCommand): AgentsCreateParams {
  return {
    ...(command.id !== undefined ? { id: command.id } : {}),
    ...(command.name !== undefined ? { name: command.name } : {}),
    ...(command.description !== undefined ? { description: command.description } : {}),
    ...(command.model !== undefined ? { model: command.model } : {}),
    ...(command.workspace !== undefined ? { workspace: command.workspace } : {}),
    ...(command.agentDir !== undefined ? { agentDir: command.agentDir } : {}),
    ...(command.enabled !== undefined ? { enabled: command.enabled } : {}),
    ...(command.systemPrompt !== undefined ? { systemPrompt: command.systemPrompt } : {}),
    ...(command.tools !== undefined ? { tools: [...command.tools] } : {}),
  } as AgentsCreateParams
}

function updateParams(command: UpdateAgentCommand): AgentsUpdateParams {
  return {
    id: command.id,
    ...(command.name !== undefined ? { name: command.name } : {}),
    ...(command.description !== undefined ? { description: command.description } : {}),
    ...(command.model !== undefined ? { model: command.model } : {}),
    ...(command.workspace !== undefined ? { workspace: command.workspace } : {}),
    ...(command.agentDir !== undefined ? { agentDir: command.agentDir } : {}),
    ...(command.enabled !== undefined ? { enabled: command.enabled } : {}),
    ...(command.systemPrompt !== undefined ? { systemPrompt: command.systemPrompt } : {}),
    ...(command.tools !== undefined ? { tools: [...command.tools] } : {}),
  }
}

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

export function createV4AgentCatalog(rpc: RpcTransport): AgentCatalog {
  return {
    async list(options) {
      await rpc.ready({ signal: options?.signal })
      const params: AgentsListParams = {}
      const result = await rpc.request<AgentsListResult>(
        AGENTS_LIST_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateAgentsListResult(result)) throw invalid(AGENTS_LIST_METHOD)
      return result.agents as Agent[]
    },
    async create(command, options) {
      const result = await rpc.request<AgentsCreateResult>(
        AGENTS_CREATE_METHOD,
        createParams(command),
        callOptions(options?.signal),
      )
      if (!validateAgentsCreateResult(result)) throw invalid(AGENTS_CREATE_METHOD)
      return result as Agent
    },
    async update(command, options) {
      const result = await rpc.request<AgentsUpdateResult>(
        AGENTS_UPDATE_METHOD,
        updateParams(command),
        callOptions(options?.signal),
      )
      if (!validateAgentsUpdateResult(result)) throw invalid(AGENTS_UPDATE_METHOD)
      return result as Agent
    },
    async remove(agentId, options) {
      const params: AgentsDeleteParams = { id: agentId }
      const result = await rpc.request<AgentsDeleteResult>(
        AGENTS_DELETE_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateAgentsDeleteResult(result)) throw invalid(AGENTS_DELETE_METHOD)
    },
  }
}
