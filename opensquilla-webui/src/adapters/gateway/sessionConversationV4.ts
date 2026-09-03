import type { RpcEventHandler } from '@/lib/rpc'
import type {
  SessionConversation,
} from '@/modules/sessionConversation'

interface SessionConversationEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
  supports?(event: string): boolean
}

export function createV4SessionConversation(
  events: SessionConversationEventTransport,
): SessionConversation {
  return {
    subscribeToolResults(listener) {
      return events.subscribe('session.event.tool_result', listener)
    },

    subscribeRoutingChanged(listener) {
      return events.subscribe('models.routing.changed', payload => {
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return
        listener(payload)
      })
    },

    supports(): boolean {
      return events.supports?.('session.event.turn_committed') !== false
    },
  }
}
