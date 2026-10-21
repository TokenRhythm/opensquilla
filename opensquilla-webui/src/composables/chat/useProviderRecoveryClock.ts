import { onScopeDispose, ref, watch, type WatchSource } from 'vue'
/** The server deadline is authoritative; this only refreshes its presentation. */
export function useProviderRecoveryClock(active: WatchSource<boolean>) {
  const now = ref(Date.now())
  let timer: ReturnType<typeof setInterval> | undefined
  watch(active, enabled => {
    if (timer) clearInterval(timer)
    timer = undefined
    now.value = Date.now()
    if (enabled) timer = setInterval(() => { now.value = Date.now() }, 1000)
  }, { immediate: true })
  onScopeDispose(() => { if (timer) clearInterval(timer) })
  return now
}
