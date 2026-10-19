import { sessionRecoverySucceeded } from './sessionBootstrapContract'
import type { SessionRecoveryProof } from './sessionRecoveryProof'
import type { SessionSubscriptionOutcome } from './useChatSessionSubscription'

interface ChatSessionRecoveryOptions {
  sessionKey: () => string
  recoveryProof: SessionRecoveryProof
  isDisposed: () => boolean
  retryLive: () => Promise<SessionSubscriptionOutcome>
  isCurrentLiveResult: (result: SessionSubscriptionOutcome) => boolean
}

/** The coordinator owns retries and may replace a lease while recovering. */
export function createChatSessionRecovery(options: ChatSessionRecoveryOptions) {
  return async (scope?: { readonly keys: readonly string[], readonly global: boolean }): Promise<boolean> => {
    const key = options.sessionKey()
    if (!key || options.isDisposed()
      || scope && (scope.global || scope.keys.length === 0 || scope.keys.some(candidate => candidate !== key))) {
      return false
    }
    const hasCurrentRecoveryProof = options.recoveryProof.capture()
    const result = await options.retryLive()
    if (options.isDisposed() || key !== options.sessionKey() || !hasCurrentRecoveryProof.isCurrent()) return false
    // A successful new lease is valid; an older successful read preceding a
    // queued reconciliation is not. The coordinator owns that distinction.
    if (result.authoritative) return hasCurrentRecoveryProof() && options.isCurrentLiveResult(result)
    return sessionRecoverySucceeded(result)
  }
}
