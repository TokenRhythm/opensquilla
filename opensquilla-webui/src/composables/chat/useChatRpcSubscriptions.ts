import type { RpcEventHandler } from '@/lib/rpc'
import {
  createConversationEventHub,
  type ConversationEventHandle,
  type ConversationEventHub,
} from '@/modules/conversationEventHub'
import type { ConversationSessionRuntime } from '@/modules/conversationSessionRuntime'
import {
  createConversationEventTransport,
  conversationEventSessionKey,
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
 * remains as a compatibility bridge while the composition root owns a shared
 * ConversationSessionRuntime. It never creates a second source when that
 * runtime is supplied.
 */
export type ChatRpcSubscriptionHandlers = {
  onEvent: (message: ConversationEventTransportMessage) => void
  onAny?: ConversationEventTransportHandlers['onAny']
  onConnectionState?: ConversationEventTransportHandlers['onConnectionState']
  onDecodeError?: ConversationEventTransportHandlers['onDecodeError']
}

export interface ChatRpcSubscriptionOptions {
  /** Return the currently visible session key for logical event fencing. */
  getSessionKey?: () => string
  /** Shared runtime owner; avoids a second event source for this composition root. */
  runtime?: Pick<ConversationSessionRuntime<ConversationEventTransportMessage, never>, 'events'>
}

export function useChatRpcSubscriptions(
  rpc: RpcSubscriptionClient,
  handlers: ChatRpcSubscriptionHandlers,
  options: ChatRpcSubscriptionOptions = {},
) {
  const hub: ConversationEventHub<ConversationEventTransportMessage> =
    options.runtime?.events
    ?? createConversationEventHub(createConversationEventTransport(rpc), {
      sessionKey: conversationEventSessionKey,
    })
  let activeHandle: ConversationEventHandle<ConversationEventTransportMessage> | null = null
  let activeKey = ''
  let detachEvent: (() => void) | null = null
  let detachAny: (() => void) | null = null
  let detachState: (() => void) | null = null
  let detachDecodeError: (() => void) | null = null

  function subscribe(): () => void {
    unsubscribe()
    // The empty-key handle preserves the existing Conversation-wide reducer
    // view when no key provider is supplied. ChatView supplies its current
    // session key, so positively tagged events from another session are fenced
    // before they reach the reducer.
    activeKey = String(options.getSessionKey?.() || '')
    activeHandle = hub.open(activeKey)
    detachEvent = activeHandle.observe(handlers.onEvent)
    if (handlers.onAny) detachAny = hub.observeAny(handlers.onAny)
    if (handlers.onConnectionState) {
      detachState = hub.observeConnectionState(handlers.onConnectionState)
    }
    if (handlers.onDecodeError) {
      detachDecodeError = hub.observeDecodeError(handlers.onDecodeError)
    }
    return unsubscribe
  }

  function unsubscribe() {
    detachEvent?.()
    detachEvent = null
    detachAny?.()
    detachAny = null
    detachState?.()
    detachState = null
    detachDecodeError?.()
    detachDecodeError = null
    activeHandle?.close()
    activeHandle = null
  }

  /** Switch the logical owner without touching the physical source. */
  function setSessionKey(key: string) {
    activeKey = String(key || '')
    if (!activeHandle) return
    detachEvent?.()
    detachEvent = null
    activeHandle.close()
    activeHandle = hub.open(activeKey)
    detachEvent = activeHandle.observe(handlers.onEvent)
  }

  /** Open an additional logical stream without acquiring another WebSocket. */
  function open(
    key: string,
    listener: (message: ConversationEventTransportMessage) => void = handlers.onEvent,
  ) {
    const handle = hub.open(key)
    const detach = handle.observe(listener)
    return {
      handle,
      unsubscribe: () => {
        detach()
        handle.close()
      },
    }
  }

  return {
    subscribe,
    unsubscribe,
    open,
    setSessionKey,
  }
}
