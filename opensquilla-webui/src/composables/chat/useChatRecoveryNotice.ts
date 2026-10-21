import { computed, type Ref } from 'vue'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import { CONNECTION_NOTICE_DELAY_MS, useDelayedNotice } from '@/composables/useDelayedNotice'

/** One quiet notice for an uninterrupted recovery, independent of phase churn. */
export function useChatRecoveryNotice(
  state: Readonly<Ref<ChatSessionRecoveryState | null>>,
  runtimeStarting?: Readonly<Ref<boolean>>,
  connectionBlocked?: Readonly<Ref<boolean>>,
) {
  const liveRecovery = computed(() => state.value === 'live-connecting' || state.value === 'live-degraded')
  // Phase changes within a category keep its timer. Moving from history to
  // live recovery starts a fresh connection grace period, not a leftover 2s one.
  const connectionVisible = useDelayedNotice(
    liveRecovery, connectionBlocked, runtimeStarting, CONNECTION_NOTICE_DELAY_MS,
  )
  const historyVisible = useDelayedNotice(
    computed(() => state.value !== null && !liveRecovery.value),
    computed(() => state.value === 'session-missing'),
    runtimeStarting,
  )
  return computed(() => connectionVisible.value || historyVisible.value)
}
