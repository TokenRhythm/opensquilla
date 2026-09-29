import { computed, reactive } from 'vue'

type MessageRating = 'up' | 'down'

// Page-local UI state only. No provider, routing or feedback service consumes
// these ratings. Stable message identities survive virtual-list remounts.
const ratings = reactive(new Map<string, MessageRating>())

export function useChatMessageFeedback(
  sessionKey: () => string | undefined,
  messageId: () => string | undefined,
) {
  const key = computed(() => {
    const session = sessionKey()
    const message = messageId()
    return session && message ? JSON.stringify([session, message]) : ''
  })
  const available = computed(() => Boolean(key.value))
  const rating = computed(() => ratings.get(key.value))

  function toggle(value: MessageRating): void {
    if (!key.value) return
    if (ratings.get(key.value) === value) ratings.delete(key.value)
    else ratings.set(key.value, value)
  }

  return { available, rating, toggle }
}
