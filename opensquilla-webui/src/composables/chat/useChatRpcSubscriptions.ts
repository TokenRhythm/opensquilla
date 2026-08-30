import type { RpcEventHandler } from '@/lib/rpc'
import {
  createConversationEventTransport,
  type ConversationEventTransportHandlers,
  type ConversationEventTransportMessage,
} from '@/adapters/gateway/conversationEventTransport'

type RpcSubscriptionClient = {
  on(event: string, handler: RpcEventHandler): () => void
}

/**
 * Composition-root bridge for the Conversation event lane.
 *
 * The bridge intentionally has no wire event names or payload DTOs. The v4
 * adapter owns those details and emits one decoded message; this small bridge
 * remains until ConversationRuntime.open() owns the subscription lifecycle.
 */
export type ChatRpcSubscriptionHandlers = {
  onEvent: (message: ConversationEventTransportMessage) => void
  onAny?: ConversationEventTransportHandlers['onAny']
  onConnectionState?: ConversationEventTransportHandlers['onConnectionState']
  onDecodeError?: ConversationEventTransportHandlers['onDecodeError']
}

export function useChatRpcSubscriptions(
  rpc: RpcSubscriptionClient,
  handlers: ChatRpcSubscriptionHandlers,
) {
  const transport = createConversationEventTransport(rpc)
  let unsubscribeTransport: (() => void) | null = null

  function subscribe(): () => void {
    unsubscribe()
    unsubscribeTransport = transport.subscribe(handlers)
    return unsubscribe
  }

  function unsubscribe() {
    unsubscribeTransport?.()
    unsubscribeTransport = null
  }

  return {
    subscribe,
    unsubscribe,
  }
}
