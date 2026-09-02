import type { RpcEventHandler } from '@/lib/rpc'
import type { ConversationEventSourceHandlers } from '@/modules/conversationEventHub'
import type {
  ConversationEvent,
  ConversationEventProjection,
} from '@/modules/conversationEvents'
import { conversationEventSessionKey } from '@/modules/conversationEvents'
import {
  conversationSemanticEventKind,
  decodeConversationEvent,
} from './conversationEventsV4'

/**
 * A semantic event message is the only event shape that leaves the v4 adapter.
 * The opaque payload remains byte-for-byte owned by the producer, while wire
 * names and aliases stop here.
 */
export type ConversationEventTransportMessage = ConversationEvent
export type { ConversationEventProjection }

export interface ConversationEventTransportHandlers
  extends ConversationEventSourceHandlers<ConversationEventTransportMessage> {
  /** One typed ingress for the Conversation reducer/application seam. */
}

/**
 * Extract the positive session identity at the adapter edge. Aliases stay
 * here; the hub can then fence a keyed handle without teaching the domain
 * module about JSON-RPC field spellings. Directory invalidations are global by
 * design and therefore return null.
 */
export { conversationEventSessionKey }

function rawSessionKey(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const value = payload as Record<string, unknown>
  for (const name of ['key', 'session_key', 'sessionKey']) {
    const candidate = value[name]
    if (typeof candidate === 'string' && candidate) return candidate
  }
  return null
}

type RpcSubscriptionClient = {
  on(event: string, handler: RpcEventHandler): () => void
}

/** Create the one WebSocket event listener used by the Conversation lane. */
export function createConversationEventTransport(rpc: RpcSubscriptionClient) {
  let detach: (() => void) | null = null

  function subscribe(handlers: ConversationEventTransportHandlers): () => void {
    detach?.()
    const onEvent: RpcEventHandler = (
      rawEvent: unknown,
      rawPayload: unknown,
      rawMeta: unknown,
    ) => {
      const eventName = typeof rawEvent === 'string' ? rawEvent : String(rawEvent ?? '')

      // `sessions.changed` has its own Contract family.  It is intentionally
      // handled here as a directory event until the Session Event lane merges
      // both manifests; it must still pass through the same single listener.
      if (eventName === 'sessions.changed') {
        handlers.onEvent?.({
          kind: 'sessions-changed',
          payload: rawPayload,
          meta: rawMeta,
        })
        return
      }

      const semanticKind = conversationSemanticEventKind(eventName)
      if (semanticKind === 'approval-requested' || semanticKind === 'approval-resolved') {
        handlers.onEvent?.({
          kind: 'approval',
          action: semanticKind === 'approval-requested' ? 'requested' : 'resolved',
          sessionKey: rawSessionKey(rawPayload),
          payload: rawPayload,
          meta: rawMeta,
        })
        return
      }

      try {
        const decoded = decodeConversationEvent(eventName, rawPayload, rawMeta)
        const { name: _wireName, ...event } = decoded
        handlers.onEvent?.({
          kind: 'conversation',
          event: event as ConversationEventProjection,
          payload: rawPayload,
          meta: rawMeta,
        })
      } catch (error) {
        // A malformed or unrelated frame must not take down the shared event
        // stream. Preserve the old wildcard observation path through the
        // `invalid` message and report the contract violation for diagnostics.
        handlers.onEvent?.({
          kind: 'invalid',
          error,
        })
        handlers.onDecodeError?.(error)
      }
    }

    const offWildcard = rpc.on('*', onEvent)
    const offState = rpc.on('_state', (state: unknown) => {
      handlers.onConnectionState?.(String(state))
    })
    detach = () => {
      offWildcard()
      offState()
      detach = null
    }
    return detach
  }

  function unsubscribe() {
    detach?.()
  }

  return { subscribe, unsubscribe }
}
