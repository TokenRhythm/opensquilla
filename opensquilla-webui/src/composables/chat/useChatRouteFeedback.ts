import { hasInjectionContext, inject, reactive } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { SESSION_CONVERSATION_KEY, type SessionConversation } from '@/modules/sessionConversation'
import { createLegacySessionConversation } from '@/adapters/gateway/sessionConversationV4'
import { useRpcStore } from '@/stores/rpc'

export type RouteFeedbackRating = 'up' | 'down'

// Per-decision selected state for the whole view. Keyed by decisionId (not
// message index) so history reloads and regenerates keep ratings attached to
// the routing decision they judged. Optimistic: the sidecar is last-write-wins
// server-side, so replaying a click after a failed call is always safe.
const selected = reactive(new Map<string, RouteFeedbackRating>())
const inFlight = reactive(new Set<string>())

export function useChatRouteFeedback(conversation?: SessionConversation) {
  const { pushToast } = useToasts()
  let sessionConversation = conversation
    ?? (hasInjectionContext() ? inject(SESSION_CONVERSATION_KEY, null) : null)

  function resolveConversation(): SessionConversation {
    if (sessionConversation) return sessionConversation
    sessionConversation = createLegacySessionConversation(
      useRpcStore() as Parameters<typeof createLegacySessionConversation>[0],
    )
    return sessionConversation
  }

  function ratingFor(decisionId: string | undefined): RouteFeedbackRating | undefined {
    return decisionId ? selected.get(decisionId) : undefined
  }

  function busy(decisionId: string | undefined): boolean {
    return !!decisionId && inFlight.has(decisionId)
  }

  /** Clicking the active thumb again revokes (neutral); clicking the other revises. */
  async function submit(decisionId: string, rating: RouteFeedbackRating): Promise<void> {
    if (inFlight.has(decisionId)) return
    const previous = selected.get(decisionId)
    const effective = previous === rating ? 'neutral' : rating

    // Optimistic flip; rolled back below if the gateway refuses.
    if (effective === 'neutral') selected.delete(decisionId)
    else selected.set(decisionId, rating)

    inFlight.add(decisionId)
    try {
      const res = await resolveConversation().submitRouteFeedback(decisionId, effective)
      if (!res?.accepted) {
        rollback(decisionId, previous)
        pushToast(
          res?.reason === 'decision_not_found'
            ? i18n.global.t('chat.routeFeedback.expired')
            : i18n.global.t('chat.routeFeedback.failed'),
          { tone: 'danger' },
        )
      }
    } catch {
      rollback(decisionId, previous)
      pushToast(i18n.global.t('chat.routeFeedback.failed'), { tone: 'danger' })
    } finally {
      inFlight.delete(decisionId)
    }
  }

  function rollback(decisionId: string, previous: RouteFeedbackRating | undefined) {
    if (previous === undefined) selected.delete(decisionId)
    else selected.set(decisionId, previous)
  }

  return { ratingFor, busy, submit }
}
