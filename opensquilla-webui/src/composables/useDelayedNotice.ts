import { onScopeDispose, readonly, ref, watch, type Ref } from 'vue'

// History feedback keeps its existing delay; reconnecting gets time for retries.
export const RECOVERY_NOTICE_DELAY_MS = 2_000
export const CONNECTION_NOTICE_DELAY_MS = 5_000

/** Delay passive feedback, not the underlying operation or connection state. */
export function useDelayedNotice(
  active: Readonly<Ref<boolean>>,
  immediate?: Readonly<Ref<boolean>>,
  suppressed?: Readonly<Ref<boolean>>,
  delayMs = RECOVERY_NOTICE_DELAY_MS,
) {
  const visible = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  function clear() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  const stop = watch(
    [active, () => immediate?.value ?? false, () => suppressed?.value ?? false],
    ([pending, urgent, hidden]) => {
      if (!pending || hidden) {
        clear()
        visible.value = false
      } else if (urgent) {
        clear()
        visible.value = true
      } else if (!visible.value && timer === null) {
        timer = setTimeout(() => {
          timer = null
          visible.value = active.value && !suppressed?.value
        }, delayMs)
      }
    },
    { immediate: true, flush: 'sync' },
  )
  onScopeDispose(() => { stop(); clear() })
  return readonly(visible)
}
