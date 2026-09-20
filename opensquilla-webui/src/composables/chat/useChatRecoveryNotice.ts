import { onScopeDispose, ref, watch, type Ref } from 'vue'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'

/** One quiet notice for an uninterrupted recovery, independent of phase churn. */
export function useChatRecoveryNotice(
  state: Readonly<Ref<ChatSessionRecoveryState | null>>,
  runtimeStarting?: Readonly<Ref<boolean>>,
) {
  const visible = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  function clear() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  const stop = watch([state, () => runtimeStarting?.value ?? false], ([value, starting]) => {
    // Desktop already owns the startup notice. A session read deadline during
    // onboarding must not present a connection failure or a futile Reconnect.
    if (value === null || starting) {
      clear()
      visible.value = false
    } else if (value === 'session-missing') {
      clear()
      visible.value = true
    } else if (!visible.value && timer === null) {
      timer = setTimeout(() => {
        timer = null
        visible.value = state.value !== null
      }, 2_000)
    }
  }, { immediate: true })
  onScopeDispose(() => { stop(); clear() })
  return visible
}
