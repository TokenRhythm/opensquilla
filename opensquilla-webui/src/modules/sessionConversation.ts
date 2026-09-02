import type { InjectionKey } from 'vue'

export type SessionConversationCapability = 'turn-committed'

export interface SessionConversationSubscription {
  close(): void
}

/**
 * Application-facing conversation seam. Wire method names, aliases and
 * connection state are owned by the Gateway Adapter. Existing conversation
 * runtime/cursor modules remain responsible for ordering and replay policy.
 */
export interface SessionConversation {
  subscribeToolResults(listener: (...args: unknown[]) => void): SessionConversationSubscription
  subscribeRoutingChanged(listener: (snapshot: unknown) => void): SessionConversationSubscription
  supports(capability: SessionConversationCapability): boolean
}

export const SESSION_CONVERSATION_KEY: InjectionKey<SessionConversation> =
  Symbol('SessionConversation')
