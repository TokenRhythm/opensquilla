import type { RpcCallOptions } from '@/lib/rpc'
import {
  type TurnSendRequest,
  type TurnCancelRequest,
  type TurnCancelResponse,
  type TurnCommandCapability,
  type TurnCommands,
  type TurnCommandRequestOptions,
} from '@/modules/turnCommands'
import type {
  ChatSendResponse,
  SessionSteerV2Params,
  SessionSteerV2Response,
} from '@/types/rpc'

/** Narrow wire port owned by this Adapter. */
export interface TurnCommandsTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  supports?(method: string): boolean
}

const CHAT_SEND_METHOD = 'chat.send'
const CHAT_ABORT_METHOD = 'chat.abort'
const STEER_METHOD = 'sessions.steer.v2'
const PENDING_INPUT_DISPATCH_METHOD = 'sessions.pending_inputs.dispatch'
const PENDING_INPUT_STEER_METHOD = 'sessions.pending_inputs.steer'

function requestOptions(options?: TurnCommandRequestOptions): RpcCallOptions | undefined {
  return options?.signal ? { signal: options.signal } : undefined
}

function forward<T>(
  transport: TurnCommandsTransport,
  method: string,
  params: Record<string, unknown>,
  options?: TurnCommandRequestOptions,
): Promise<T> {
  const rpcOptions = requestOptions(options)
  return rpcOptions
    ? transport.request<T>(method, params, rpcOptions)
    : transport.request<T>(method, params)
}

/**
 * Adapt semantic turn commands to the unchanged v4 JSON wire.
 *
 * This is intentionally a compatibility Adapter, not a second implementation:
 * it only selects legacy method aliases and forwards the exact payloads.
 */
export function createV4TurnCommands(transport: TurnCommandsTransport): TurnCommands {
  const supportsMethod = (method: string): boolean => (
    transport.supports?.(method) ?? false
  )

  return {
    send: async (
      request: TurnSendRequest,
      options?: TurnCommandRequestOptions,
    ): Promise<ChatSendResponse> => {
      if (request.kind === 'pending-input') {
        return forward<ChatSendResponse>(
          transport,
          PENDING_INPUT_DISPATCH_METHOD,
          request.params as unknown as Record<string, unknown>,
          options,
        )
      }
      return forward<ChatSendResponse>(
        transport,
        CHAT_SEND_METHOD,
        request.params as unknown as Record<string, unknown>,
        options,
      )
    },

    cancel: (
      request: TurnCancelRequest,
      options?: TurnCommandRequestOptions,
    ) => forward<TurnCancelResponse>(
      transport,
      CHAT_ABORT_METHOD,
      request as unknown as Record<string, unknown>,
      options,
    ),

    steer: (
      request: SessionSteerV2Params,
      options?: TurnCommandRequestOptions,
    ) => forward<SessionSteerV2Response>(
      transport,
      request.pendingInputId ? PENDING_INPUT_STEER_METHOD : STEER_METHOD,
      request as unknown as Record<string, unknown>,
      options,
    ),

    supports: (capability: TurnCommandCapability): boolean => {
      if (capability === 'same-turn-steer') return supportsMethod(STEER_METHOD)
      return supportsMethod(PENDING_INPUT_STEER_METHOD)
    },
  }
}

/**
 * Keep the method mapping testable without exposing a generic RPC client from
 * the application Module.  This helper is useful for transitional call-site
 * tests while production composition uses `createPrivateGatewayTransports`.
 */
export function createV4TurnCommandsFromRpcClient(client: {
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  supportsMethod?(method: string): boolean
}, supportsMethod?: (method: string) => boolean): TurnCommands {
  return createV4TurnCommands({
    request: (method, params, options) => options
      ? client.call(method, params, options)
      : client.call(method, params),
    supports: method => supportsMethod?.(method)
      ?? client.supportsMethod?.(method)
      ?? false,
  })
}
