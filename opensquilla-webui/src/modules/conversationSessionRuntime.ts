import {
  createConversationEventHub,
  type ConversationEventHub,
  type ConversationEventHubOptions,
  type ConversationEventSource,
} from './conversationEventHub'
import {
  createConversationRuntime,
  type ConversationRuntime,
} from './conversationRuntime'
import {
  createConversationSubscriptionLifecycle,
  type ConversationSubscriptionLifecycle,
} from './conversationSubscriptionLifecycle'

/**
 * The transport/application seam for one Conversation composition root.
 *
 * The object deliberately contains policies and ownership, not Vue refs or
 * wire names. A view may project `cursor` state into refs, while adapters own
 * the event source and subscription calls. Keeping the three related pieces
 * together prevents a second composable from accidentally creating a second
 * lease registry or cursor policy for the same physical conversation.
 */
export interface ConversationSessionRuntime<TEvent, TSubscriptionOutcome> {
  /** Pure session-epoch/generation/sequence policy. */
  readonly cursor: ConversationRuntime
  /** One physical source, multiplexed into logical event handles. */
  readonly events: ConversationEventHub<TEvent>
  /** One owner for subscription attempts and logical leases. */
  readonly subscriptions: ConversationSubscriptionLifecycle<TSubscriptionOutcome>
  /** Release the event source and invalidate an in-flight subscription. */
  dispose(): void
}

export interface ConversationSessionRuntimeOptions<TEvent> {
  source: ConversationEventSource<TEvent>
  events?: ConversationEventHubOptions<TEvent>
  cursor?: ConversationRuntime
}

/**
 * Build the shared Conversation services once at the composition root.
 *
 * `source` is an adapter boundary: this module does not import RpcClient or
 * generated wire types. Tests and future transports can provide an in-memory
 * source without changing the application-facing modules.
 */
export function createConversationSessionRuntime<TEvent, TSubscriptionOutcome>(
  options: ConversationSessionRuntimeOptions<TEvent>,
): ConversationSessionRuntime<TEvent, TSubscriptionOutcome> {
  const cursor = options.cursor ?? createConversationRuntime()
  const events = createConversationEventHub(options.source, options.events)
  const subscriptions = createConversationSubscriptionLifecycle<TSubscriptionOutcome>()
  let disposed = false

  return {
    cursor,
    events,
    subscriptions,
    dispose() {
      if (disposed) return
      disposed = true
      subscriptions.cancel()
      events.dispose()
    },
  }
}
