import type { RpcEventHandler } from '@/lib/rpc'
import {
  decodeConversationEvent,
  type DecodedConversationEvent,
} from './conversationEventsV4'

/**
 * A decoded event message is the only event shape that leaves the v4 adapter.
 * `wireName` and `payload` are retained for compatibility with the existing
 * reducer (which still understands a few legacy spellings), while `decoded`
 * carries the validated/canonical projection for new consumers.  Keeping the
 * raw values here avoids forcing a behavior change while the reducer is moved
 * behind the ConversationRuntime seam in the next slice.
 */
export type ConversationEventTransportMessage =
  | {
      kind: 'conversation'
      wireName: string
      decoded: DecodedConversationEvent
      payload: unknown
      meta: unknown
    }
  | {
      kind: 'sessions-changed'
      wireName: 'sessions.changed'
      decoded: null
      payload: unknown
      meta: unknown
    }
  | {
      /** A malformed/unrelated frame kept for the legacy wildcard reducer. */
      kind: 'invalid'
      wireName: string
      decoded: null
      payload: unknown
      meta: unknown
      error: unknown
    }

export interface ConversationEventTransportHandlers {
  /** One typed ingress for the Conversation reducer/application seam. */
  onEvent?: (message: ConversationEventTransportMessage) => void
  /** Preserve raw observation for diagnostics and watchdogs only. */
  onAny?: (rawEvent: string, rawPayload: unknown) => void
  onConnectionState?: (state: string) => void
  onDecodeError?: (error: unknown, rawEvent: string, rawPayload: unknown) => void
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
          wireName: 'sessions.changed',
          decoded: null,
          payload: rawPayload,
          meta: rawMeta,
        })
        handlers.onAny?.(eventName, rawPayload)
        return
      }

      try {
        const decoded = decodeConversationEvent(eventName, rawPayload, rawMeta)
        handlers.onEvent?.({
          kind: 'conversation',
          wireName: eventName,
          decoded,
          payload: rawPayload,
          meta: rawMeta,
        })
      } catch (error) {
        // A malformed or unrelated frame must not take down the shared event
        // stream. Preserve the old wildcard observation path through the
        // `invalid` message and report the contract violation for diagnostics.
        handlers.onEvent?.({
          kind: 'invalid',
          wireName: eventName,
          decoded: null,
          payload: rawPayload,
          meta: rawMeta,
          error,
        })
        handlers.onDecodeError?.(error, eventName, rawPayload)
      }
      handlers.onAny?.(eventName, rawPayload)
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
