/**
 * Logical subscription ownership for one Conversation stream.
 *
 * A lease is deliberately independent from the physical WebSocket.  Route
 * changes may retire an old lease while a replacement lease is acquiring on
 * the same connection; only the generation that actually sent the subscribe
 * frame may release it.  Keeping these invariants in a small module makes the
 * lifecycle testable without Vue, RpcClient, or Gateway implementation code.
 */
export type ConversationSubscriptionLeaseState =
  | 'acquiring'
  | 'active'
  | 'releasing'
  | 'retired'

export interface ConversationSubscriptionLease {
  readonly token: symbol
  readonly key: string
  state: ConversationSubscriptionLeaseState
  socketGeneration: number | null
  releasePromise: Promise<void> | null
}

export interface ConversationSubscriptionLeaseRegistry {
  acquire(key: string): ConversationSubscriptionLease
  activate(lease: ConversationSubscriptionLease): void
  retire(lease: ConversationSubscriptionLease): void
  retirePriorGenerations(currentGeneration?: number): void
  latestReleasable(key: string): ConversationSubscriptionLease | null
  clearActive(lease: ConversationSubscriptionLease): void
  readonly active: ConversationSubscriptionLease | null
  readonly size: number
}

export function createConversationSubscriptionLeaseRegistry(): ConversationSubscriptionLeaseRegistry {
  const leases = new Set<ConversationSubscriptionLease>()
  let activeLease: ConversationSubscriptionLease | null = null

  function retire(lease: ConversationSubscriptionLease) {
    if (lease.state === 'retired') return
    lease.state = 'retired'
    leases.delete(lease)
    if (activeLease === lease) activeLease = null
  }

  function activate(lease: ConversationSubscriptionLease) {
    if (lease.state !== 'acquiring') return
    // Gateway registration is a set keyed by (connection, session). A newer
    // successful acquire for the same key subsumes earlier non-releasing
    // leases, while a closing A1 remains distinct from a later A2 acquire.
    for (const candidate of leases) {
      if (
        candidate !== lease
        && candidate.key === lease.key
        && (candidate.state === 'acquiring' || candidate.state === 'active')
      ) {
        retire(candidate)
      }
    }
    lease.state = 'active'
    activeLease = lease
  }

  function retirePriorGenerations(currentGeneration?: number) {
    if (typeof currentGeneration !== 'number') return
    for (const lease of leases) {
      if (
        lease.socketGeneration !== null
        && lease.socketGeneration !== currentGeneration
      ) {
        retire(lease)
      }
    }
  }

  return {
    acquire(key: string) {
      const lease: ConversationSubscriptionLease = {
        token: Symbol('conversation-subscription'),
        key,
        state: 'acquiring',
        socketGeneration: null,
        releasePromise: null,
      }
      leases.add(lease)
      activeLease = lease
      return lease
    },
    activate,
    retire,
    retirePriorGenerations,
    latestReleasable(key: string) {
      const matches = [...leases].filter(lease => (
        lease.key === key
        && (lease.state === 'acquiring' || lease.state === 'active')
      ))
      return matches.length > 0 ? matches[matches.length - 1]! : null
    },
    clearActive(lease: ConversationSubscriptionLease) {
      if (activeLease === lease) activeLease = null
    },
    get active() {
      return activeLease
    },
    get size() {
      return leases.size
    },
  }
}

