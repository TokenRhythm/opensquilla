import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  COMMANDS_LIST_FOR_SURFACE_METHOD,
  type Params as CommandCatalogParams,
  type Result as CommandCatalogWireResult,
} from '@/contracts/generated/v4/commandsListForSurface'
import { validateResult as validateCommandCatalogResult } from '@/contracts/generated/v4/commandsListForSurfaceValidators.mjs'
import type { CommandCatalog, CommandCatalogResult } from '@/modules/commandCatalog'

interface CommandCatalogTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

export function createV4CommandCatalog(transport: CommandCatalogTransport): CommandCatalog {
  return {
    async list(surface, options) {
      const params: CommandCatalogParams = { surface }
      const callOptions: RpcCallOptions | undefined = options
        ? {
            signal: options.signal,
            timeoutMs: options.timeoutMs,
            timeoutAction: 'reject',
            abortAction: 'reject',
          }
        : undefined
      const raw = callOptions
        ? await transport.request<CommandCatalogWireResult>(
            COMMANDS_LIST_FOR_SURFACE_METHOD,
            params,
            callOptions,
          )
        : await transport.request<CommandCatalogWireResult>(
            COMMANDS_LIST_FOR_SURFACE_METHOD,
            params,
          )
      if (!validateCommandCatalogResult(raw)) {
        throw new Error(`${COMMANDS_LIST_FOR_SURFACE_METHOD} returned an invalid response`)
      }
      return raw as CommandCatalogResult
    },
  }
}
