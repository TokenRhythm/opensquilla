import { createV4SessionConversation } from '@/adapters/gateway/sessionConversationV4'
import type { RpcCallOptions, RpcConnectionWaitOptions, RpcEventHandler } from '@/lib/rpc'
import type { SessionConversation } from '@/modules/sessionConversation'

export interface SessionConversationTestRpc {
  ready?(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ): Promise<void>
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  on?(event: string, handler: RpcEventHandler): (() => void) | void
  hasRpcMethod?(method: string): boolean
  hasRpcEvent?(event: string): boolean
}

function unconfigured(method: keyof SessionConversation): never {
  throw new Error(`SessionConversation.${method} was not configured for this test`)
}

/** Build a narrow semantic test double without leaking wire method names into consumer tests. */
export function sessionConversationTestDouble(
  overrides: Partial<SessionConversation> = {},
): SessionConversation {
  return {
    subscribeToolResults: () => unconfigured('subscribeToolResults'),
    subscribeRoutingChanged: () => unconfigured('subscribeRoutingChanged'),
    supports: () => false,
    ...overrides,
  }
}

/** Adapt legacy wire-oriented spies to the production domain Adapter in tests. */
export function sessionConversationFromTestRpc(
  rpc: SessionConversationTestRpc,
): SessionConversation {
  return createV4SessionConversation(
    {
      subscribe(event, handler) {
        const close = rpc.on?.(event, handler)
        return { close: typeof close === 'function' ? close : () => {} }
      },
      supports: event => rpc.hasRpcEvent?.(event) !== false,
    },
  )
}
