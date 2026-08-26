import { computed, ref, watch, type Ref } from 'vue'
import type { ChatRenderedMessage } from '@/types/chat'

interface DeliverableUpdateIndicatorOptions {
  sessionKey: Readonly<Ref<string>>
  messages: Readonly<Ref<readonly ChatRenderedMessage[]>>
  isStreaming: Readonly<Ref<boolean>>
}

interface AppliedMutationMarker {
  key: string
  restoredFromHistory: boolean
  turnKey: string
}

function appliedMutationMarkers(
  messages: readonly ChatRenderedMessage[],
): AppliedMutationMarker[] {
  return messages.flatMap((message, index) => {
    const mutation = message.turnOutcome?.documentMutationOutcome
    if (mutation?.status !== 'applied') return []
    const identity = mutation.resultRevisionId
      || mutation.changeSetId
      || mutation.attemptId
      || message.turnId
      || message.messageId
      || message.id
      || `message-${message.sourceIndex ?? index}`
    return [{
      key: String(identity),
      restoredFromHistory: message.restoredFromHistory === true,
      turnKey: String(message.turnKey || ''),
    }]
  })
}

function latestUserTurnKey(messages: readonly ChatRenderedMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index]
    if (message?.displayRole !== 'user') continue
    const turnKey = String(message.turnKey || '')
    if (turnKey) return turnKey
  }
  return ''
}

/**
 * Keeps a small, session-local unread marker for newly applied document edits.
 * Restored history establishes a baseline and never creates a fresh marker.
 */
export function useDeliverableUpdateIndicator(
  options: DeliverableUpdateIndicatorOptions,
) {
  const hasNewDeliverable = ref(false)
  const markers = computed(() => appliedMutationMarkers(options.messages.value))
  const latestTurnKey = computed(() => latestUserTurnKey(options.messages.value))
  let observedSessionKey = ''
  let observedMutationKeys = new Set<string>()
  let liveTurnKey = ''

  watch(
    [options.sessionKey, markers, latestTurnKey, options.isStreaming],
    ([sessionKey, nextMarkers, nextTurnKey, isStreaming]) => {
    if (sessionKey !== observedSessionKey) {
      observedSessionKey = sessionKey
      observedMutationKeys = new Set(nextMarkers.map(marker => marker.key))
      liveTurnKey = isStreaming ? nextTurnKey : ''
      hasNewDeliverable.value = false
      return
    }

    if (isStreaming && nextTurnKey) liveTurnKey = nextTurnKey
    const hasFreshMutation = nextMarkers.some(marker => (
      !observedMutationKeys.has(marker.key)
      && (
        !marker.restoredFromHistory
        || (Boolean(liveTurnKey) && marker.turnKey === liveTurnKey)
      )
    ))
    for (const marker of nextMarkers) observedMutationKeys.add(marker.key)
    if (hasFreshMutation) {
      hasNewDeliverable.value = true
      liveTurnKey = ''
    }
  },
  { immediate: true },
  )

  function acknowledge() {
    hasNewDeliverable.value = false
  }

  return {
    hasNewDeliverable,
    acknowledge,
  }
}
