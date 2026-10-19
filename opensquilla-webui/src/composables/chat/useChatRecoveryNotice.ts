import { computed, type Ref } from 'vue'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'

/** Recovery is handled locally by the composer; only a missing resource is a notice. */
export function useChatRecoveryNotice(
  state: Readonly<Ref<ChatSessionRecoveryState | null>>,
  runtimeStarting?: Readonly<Ref<boolean>>,
  _connectionBlocked?: Readonly<Ref<boolean>>,
  _liveRecoveryTerminal?: Readonly<Ref<boolean>>,
) {
  // Terminal recovery still owns its retry admission/deadline. Hiding passive
  // status must neither restart it nor remove the composer's explicit retry.
  // Global transport feedback belongs to App, never this per-session surface.
  return computed(() => state.value === 'session-missing' && !runtimeStarting?.value)
}
