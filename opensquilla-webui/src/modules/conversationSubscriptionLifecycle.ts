import {
  createConversationSubscriptionLeaseRegistry,
  type ConversationSubscriptionLease,
  type ConversationSubscriptionLeaseRegistry,
} from './conversationSubscriptionLease'

export interface ConversationSubscriptionIdentity {
  key: string
  sinceStreamGeneration: string | null
  sinceStreamSeq: number
  bootstrapGeneration: number
  bootstrapAttempt: number
}

export interface ConversationSubscriptionAttempt
  extends ConversationSubscriptionIdentity {
  readonly id: number
  readonly token: symbol
  readonly controller: AbortController
  readonly lease: ConversationSubscriptionLease
}

export interface ConversationSubscriptionLifecycle<TOutcome> {
  start(
    identity: ConversationSubscriptionIdentity,
    externalSignal: AbortSignal | undefined,
    run: (attempt: ConversationSubscriptionAttempt) => Promise<TOutcome>,
  ): Promise<TOutcome>
  cancel(): void
  isCurrent(attempt: ConversationSubscriptionAttempt, key: string): boolean
  finish(attempt: ConversationSubscriptionAttempt): void
  retirePriorGenerations(currentGeneration?: number): void
  readonly leases: ConversationSubscriptionLeaseRegistry
}

type ActiveOperation<TOutcome> = {
  attempt: ConversationSubscriptionAttempt
  promise: Promise<TOutcome>
}

function sameIdentity(
  left: ConversationSubscriptionIdentity,
  right: ConversationSubscriptionIdentity,
): boolean {
  return left.key === right.key
    && left.sinceStreamGeneration === right.sinceStreamGeneration
    && left.sinceStreamSeq === right.sinceStreamSeq
    && left.bootstrapGeneration === right.bootstrapGeneration
    && left.bootstrapAttempt === right.bootstrapAttempt
}

/**
 * Own the cancellation/identity boundary for one Conversation subscription.
 *
 * The transport/application callback receives a stable attempt object and is
 * free to perform its existing subscribe → snapshot → hydrate work. The
 * lifecycle module owns the part that must not be duplicated by each caller:
 * deduplication, abort relays, monotonic attempt fencing, and lease registry
 * access. It never knows RPC method names or Vue state.
 */
export function createConversationSubscriptionLifecycle<TOutcome>(): ConversationSubscriptionLifecycle<TOutcome> {
  const leases = createConversationSubscriptionLeaseRegistry()
  let nextId = 0
  let currentId = 0
  let active: ActiveOperation<TOutcome> | null = null

  function cancel() {
    currentId += 1
    active?.attempt.controller.abort()
    active = null
  }

  function finish(attempt: ConversationSubscriptionAttempt) {
    if (active?.attempt.id === attempt.id) active = null
  }

  function start(
    identity: ConversationSubscriptionIdentity,
    externalSignal: AbortSignal | undefined,
    run: (attempt: ConversationSubscriptionAttempt) => Promise<TOutcome>,
  ): Promise<TOutcome> {
    if (active && sameIdentity(active.attempt, identity)) return active.promise
    cancel()

    const controller = new AbortController()
    const relayAbort = () => controller.abort()
    if (externalSignal?.aborted) controller.abort()
    else externalSignal?.addEventListener('abort', relayAbort, { once: true })

    const attempt: ConversationSubscriptionAttempt = {
      ...identity,
      id: ++nextId,
      token: Symbol('conversation-subscription-attempt'),
      controller,
      lease: leases.acquire(identity.key),
    }
    currentId = attempt.id

    let promise: Promise<TOutcome>
    try {
      promise = run(attempt)
    } catch (error) {
      promise = Promise.reject(error)
    }
    const settled = promise.finally(() => {
      externalSignal?.removeEventListener('abort', relayAbort)
      finish(attempt)
    })
    active = { attempt, promise: settled }
    return settled
  }

  return {
    start,
    cancel,
    isCurrent(attempt, key) {
      return currentId === attempt.id
        && attempt.key === key
        && !attempt.controller.signal.aborted
    },
    finish,
    retirePriorGenerations(currentGeneration) {
      leases.retirePriorGenerations(currentGeneration)
    },
    leases,
  }
}
