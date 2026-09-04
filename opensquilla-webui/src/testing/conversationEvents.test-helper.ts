import {
  createConversationEventHub,
  type ConversationEventHub,
  type ConversationEventSourceHandlers,
} from '@/modules/conversationEventHub'
import {
  conversationEventSessionKey,
  type ConversationEvent,
} from '@/modules/conversationEvents'
import type { ConversationEventData } from '@/modules/conversationEventContent'

export interface ConversationEventsTestHarness {
  readonly events: ConversationEventHub<ConversationEvent>
  emit(event: ConversationEvent): void
  emitToolResult(payload: ConversationEventData): void
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
      const sessionKey = payload.key ?? null
      emit({
        kind: 'conversation',
        event: {
          kind: 'known',
          semanticKind: 'tool-result',
          payload,
          meta: {},
          sessionKey,
          taskId: null,
          turnId: null,
          streamGeneration: null,
          streamSeq: null,
          connectionSeq: null,
          generationEpoch: null,
        },
      })
    },
  }
}
