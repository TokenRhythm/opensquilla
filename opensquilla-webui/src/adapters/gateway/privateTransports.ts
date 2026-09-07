import type {
  TransportCallOptions,
  TransportConnectionWaitOptions,
  TransportEventHandler,
} from './transportTypes'

/**
 * Raw v4 transport capabilities.
 *
 * These interfaces are intentionally private to Gateway Adapters. Domain
 * Modules must expose typed operations instead of forwarding method names,
 * event names, URLs, or wire payloads to Vue code.
 */
export interface RpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  ready(options?: TransportReadyOptions): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
  readonly generation: number
}

export type RpcRequester = Pick<RpcTransport, 'request'>

export interface EventTransport {
  subscribe(event: string, handler: TransportEventHandler): TransportSubscription
  supports(event: string): boolean
}

export interface TransportReadyOptions extends TransportConnectionWaitOptions {
  timeoutMs?: number
  signal?: AbortSignal
}

export interface TransportSubscription {
  close(): void
}

export interface GatewayTransports {
  readonly rpc: RpcTransport
  readonly events: EventTransport
}

interface RpcStoreTransportSource {
  readonly connectionGeneration: number
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  on(event: string, handler: TransportEventHandler): () => void
  hasRpcMethod(method: string): boolean
  hasRpcEvent(event: string): boolean
  rememberUnsupportedMethod(method: string): void
  ready(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: TransportConnectionWaitOptions,
  ): Promise<void>
}

/** Create the only generic wire-level capabilities exposed to v4 Adapters. */
export function createPrivateGatewayTransports(
  source: RpcStoreTransportSource,
): GatewayTransports {
  return {
    rpc: {
      request(method, params, options) {
        return source.call(method, params, options)
      },
      ready(options) {
        return source.ready(
          options?.timeoutMs,
          options?.signal,
          options ? {
            timeoutAction: options.timeoutAction,
            abortAction: options.abortAction,
          } : undefined,
        )
      },
      supports(method) {
        return source.hasRpcMethod(method)
      },
      markUnsupported(method) {
        source.rememberUnsupportedMethod(method)
      },
      get generation() {
        return source.connectionGeneration
      },
    },
    events: {
      subscribe(event, handler) {
        const unsubscribe = source.on(event, handler)
        let closed = false
        return {
          close() {
            if (closed) return
            closed = true
            unsubscribe()
          },
        }
      },
      supports(event) {
        return source.hasRpcEvent(event)
      },
    },
  }
}
