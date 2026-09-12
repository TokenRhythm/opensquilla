import type { ConversationEventSourceHandlers } from '@/modules/conversationEventHub'
import type {
  ConversationEvent,
  ConversationEventProjection,
} from '@/modules/conversationEvents'
import { conversationEventSessionKey } from '@/modules/conversationEvents'
import {
  conversationSemanticEventKind,
  CONVERSATION_EVENT_WIRE_NAMES,
  decodeConversationEvent,
} from './conversationEventsV4'
import type { TransportEventHandler, TransportConsumptionHandler, TransportGapHandler } from './transportTypes'
import { projectConversationContent, projectConversationEvent } from './conversationContentV4'

interface ConversationEventWireSource {
  subscribeConsumed?(event: string, handler: TransportConsumptionHandler): { close(): void }
  subscribeGap?(handler: TransportGapHandler): { close(): void }
  subscribe(
    event: string,
    handler: TransportEventHandler,
  ): { close(): void }
}

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

/** Create the one WebSocket event listener used by the Conversation lane. */
export function createConversationEventTransport(events: ConversationEventWireSource) {
  let detach: (() => void) | null = null

  function subscribe(handlers: ConversationEventTransportHandlers): () => void {
    detach?.()
    const onEvent = (
      rawEvent: unknown,
      rawPayload: unknown,
      rawMeta: unknown,
    ) => {
      const eventName = typeof rawEvent === 'string' ? rawEvent : String(rawEvent ?? '')

      // `sessions.changed` has its own Contract family.  It is intentionally
      // handled here as a directory event until the Session Event lane merges
      // both manifests; it must still pass through the same single listener.
      if (eventName === 'sessions.changed') {
        return handlers.onEvent?.({
          kind: 'sessions-changed',
          payload: projectConversationContent(rawPayload),
        })
      }

      const semanticKind = conversationSemanticEventKind(eventName)
      if (semanticKind === 'approval-requested' || semanticKind === 'approval-resolved') {
        return handlers.onEvent?.({
          kind: 'approval',
          action: semanticKind === 'approval-requested' ? 'requested' : 'resolved',
          sessionKey: rawSessionKey(rawPayload),
          payload: projectConversationContent(rawPayload),
        })
      }

      let projected: ConversationEventProjection
      try {
        projected = projectConversationEvent(decodeConversationEvent(eventName, rawPayload, rawMeta))
      } catch (error) {
        // A malformed or unrelated frame must not take down the shared event
        // stream. Preserve the old wildcard observation path through the
        // `invalid` message and report the contract violation for diagnostics.
        const result = handlers.onEvent?.({
          kind: 'invalid',
          error,
        })
        handlers.onDecodeError?.(error)
        return result
      }
      // A consumer failure is not a malformed frame, and must not dispatch a
      // second synthetic event that could manufacture consumption proof.
      return handlers.onEvent?.({ kind: 'conversation', event: projected })
    }

    const consumed = [...new Set([...CONVERSATION_EVENT_WIRE_NAMES, 'sessions.changed'])].map(name =>
      events.subscribeConsumed?.(name, async (payload, meta) => {
        const result = await onEvent(name, payload, meta)
        if (result !== 'applied' && result !== 'dirty') throw new Error('No conversation consumer owns this delivery.')
        return result
      }),
    )
    const wildcard = events.subscribe('*', (event, payload, meta) => {
      if (events.subscribeConsumed && meta && typeof meta === 'object' && 'flow' in meta) return
      try {
        void Promise.resolve(onEvent(event, payload, meta)).catch(error => handlers.onDecodeError?.(error))
      } catch (error) {
        handlers.onDecodeError?.(error)
      }
    })
    const gap = events.subscribeGap?.(async detail => {
      const value = detail && typeof detail === 'object' ? detail as Record<string, unknown> : {}
      const keys = Array.isArray(value.keys) ? value.keys.filter((key): key is string => typeof key === 'string' && key.length > 0) : []
      // Unscoped legacy sequence gaps are global; never silently narrow them
      // to whichever session happens to be visible.
      const global = value.global === true || !Array.isArray(value.keys)
      return handlers.onRecoveryRequired?.({ keys: [...new Set(keys)], global }) ?? false
    })
    const state = events.subscribe('_state', (connectionState: unknown) => {
      handlers.onConnectionState?.(String(connectionState))
    })
    detach = () => {
      wildcard.close()
      for (const subscription of consumed) subscription?.close()
      gap?.close()
      state.close()
      detach = null
    }
    return detach
  }

  function unsubscribe() {
    detach?.()
  }

  return { subscribe, unsubscribe }
}
