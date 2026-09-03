import {
  createConversationEventHub,
  type ConversationEventHub,
  type ConversationEventSourceHandlers,
} from '@/modules/conversationEventHub'
import {
  conversationEventSessionKey,
  type ConversationEvent,
} from '@/modules/conversationEvents'

export interface ConversationEventsTestHarness {
  readonly events: ConversationEventHub<ConversationEvent>
  emit(event: ConversationEvent): void
  emitToolResult(payload: Readonly<Record<string, unknown>>): void
}

/** Domain-level Conversation event source for consumer tests. */
export function createConversationEventsTestHarness(): ConversationEventsTestHarness {
  let sourceHandlers: ConversationEventSourceHandlers<ConversationEvent> | null = null
  const events = createConversationEventHub<ConversationEvent>({
    subscribe(handlers) {
      sourceHandlers = handlers
      return () => {
        if (sourceHandlers === handlers) sourceHandlers = null
      }
    },
  }, { sessionKey: conversationEventSessionKey })

  const emit = (event: ConversationEvent) => {
    sourceHandlers?.onEvent?.(event)
  }

  return {
    events,
    emit,
    emitToolResult(payload) {
      const sessionKey = typeof payload.sessionKey === 'string'
        ? payload.sessionKey
        : typeof payload.session_key === 'string'
          ? payload.session_key
          : null
      emit({
        kind: 'conversation',
        event: {
          kind: 'known',
          semanticKind: 'tool-result',
          isKnown: true,
          payload,
          rawPayload: payload,
          meta: null,
          sessionKey,
          taskId: null,
          turnId: null,
          streamGeneration: null,
          streamSeq: null,
          connectionSeq: null,
          generationEpoch: null,
          schemaVersion: 1,
          legacy: false,
        },
        payload,
        meta: null,
      })
    },
  }
}
