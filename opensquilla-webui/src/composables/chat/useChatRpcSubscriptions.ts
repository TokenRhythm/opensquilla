import {
  type ConversationEventHandle,
  type ConversationEventHub,
  type ConversationEventListener,
  type ConversationRecoveryScope,
} from '@/modules/conversationEventHub'
import type { ConversationSessionRuntime } from '@/modules/conversationSessionRuntime'
import type { ConversationEvent } from '@/modules/conversationEvents'

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
  onEvent: ConversationEventListener<ConversationEvent>
  onRecoveryRequired?: (scope: ConversationRecoveryScope) => Promise<boolean>
  onConnectionState?: (state: string) => void
  onDecodeError?: (error: unknown) => void
}

export interface ChatRpcSubscriptionOptions {
  /** Return the owned session key. An explicit empty key has no event owner;
   * omitting the provider retains the legacy Conversation-wide subscription. */
  getSessionKey?: () => string
  /** Shared runtime owner; avoids a second event source for this composition root. */
  runtime: Pick<ConversationSessionRuntime<ConversationEvent, never>, 'events'>
}

export function useChatRpcSubscriptions(
  handlers: ChatRpcSubscriptionHandlers,
  options: ChatRpcSubscriptionOptions,
) {
  const hub: ConversationEventHub<ConversationEvent> = options.runtime.events
  let activeHandle: ConversationEventHandle<ConversationEvent> | null = null
  let activeKey = ''
  let detachEvent: (() => void) | null = null
  let detachState: (() => void) | null = null
  let detachDecodeError: (() => void) | null = null
  let detachRecovery: (() => void) | null = null
  let subscribed = false
  let revision = 0

  function attachCurrentOwner() {
    if (!subscribed || (options.getSessionKey && !activeKey)) return
    const version = revision
    const key = activeKey
    const current = () => subscribed && revision === version && activeKey === key
    // No provider is the deliberate legacy global subscriber. A draft/side
    // host supplying an empty key must never silently become that subscriber.
    activeHandle = hub.open(activeKey)
    detachEvent = activeHandle.observe(handlers.onEvent)
    if (handlers.onConnectionState) {
      detachState = hub.observeConnectionState(state => { if (current()) handlers.onConnectionState!(state) })
    }
    if (handlers.onDecodeError) {
      detachDecodeError = hub.observeDecodeError(error => { if (current()) handlers.onDecodeError!(error) })
    }
    if (handlers.onRecoveryRequired) {
      detachRecovery = hub.observeRecoveryRequired(async scope => {
        if (!current() || (key && !scope.global && !scope.keys.includes(key))) return false
        const recovered = await handlers.onRecoveryRequired!(scope)
        return current() && recovered
      })
    }
  }

  function subscribe(): () => void {
    unsubscribe()
    subscribed = true
    activeKey = String(options.getSessionKey?.() || '')
    attachCurrentOwner()
    return unsubscribe
  }

  function detachCurrentOwner() {
    revision++
    detachEvent?.()
    detachEvent = null
    detachState?.()
    detachState = null
    detachDecodeError?.()
    detachDecodeError = null
    detachRecovery?.()
    detachRecovery = null
    activeHandle?.close()
    activeHandle = null
  }

  function unsubscribe() {
    subscribed = false
    detachCurrentOwner()
  }

  /** Switch the logical owner without touching the physical source. */
  function setSessionKey(key: string) {
    const nextKey = String(key || '')
    if (nextKey === activeKey) return
    detachCurrentOwner()
    activeKey = nextKey
    attachCurrentOwner()
  }

  /** Open an additional logical stream without acquiring another WebSocket. */
  function open(
    key: string,
    listener: ConversationEventListener<ConversationEvent> = handlers.onEvent,
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
