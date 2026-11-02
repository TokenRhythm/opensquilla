import { computed, inject, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import type { UsageSnapshot } from '@/types/usage'
import { requestUsageSnapshot } from './useUsageQuery'

/** Read the same calendar-day totals as the Usage page, only while the menu is open. */
export function useTodayUsage(open: Ref<boolean>) {
  const observability = inject(OBSERVABILITY_KEY, null)
  const snapshot = shallowRef<UsageSnapshot | null>(null)
  const loading = ref(false)
  const error = ref(false)
  let controller: AbortController | null = null
  let midnightTimer: ReturnType<typeof setTimeout> | null = null

  const tokens = computed(() => snapshot.value
    ? snapshot.value.totals.input + snapshot.value.totals.output
    : null)
  const approximate = computed(() => snapshot.value !== null
    && (snapshot.value.mode !== 'ledger_exact' || snapshot.value.timezoneFallback !== null))

  function cancel() {
    controller?.abort()
    controller = null
    if (midnightTimer !== null) clearTimeout(midnightTimer)
    midnightTimer = null
    loading.value = false
  }

  async function refresh() {
    cancel()
    // Every opening gets a fresh day and connection snapshot. In particular,
    // yesterday's "today" must never become a fallback after midnight.
    snapshot.value = null
    error.value = false
    if (!open.value) return
    if (!observability) {
      error.value = true
      return
    }

    // Usage queries default to this browser's timezone. A menu left open
    // overnight must also change its calendar day without background polling.
    const midnight = new Date()
    midnight.setHours(24, 0, 0, 0)
    midnightTimer = setTimeout(() => { void refresh() }, midnight.getTime() - Date.now())

    const request = new AbortController()
    controller = request
    loading.value = true
    try {
      const result = await requestUsageSnapshot(observability, 'today', {
        days: false,
        models: false,
        sessions: false,
        signal: request.signal,
      })
      if (controller === request && !request.signal.aborted) snapshot.value = result
    } catch {
      if (controller === request && !request.signal.aborted) error.value = true
    } finally {
      if (controller === request) {
        controller = null
        loading.value = false
      }
    }
  }

  watch(open, refresh, { immediate: true, flush: 'sync' })

  onScopeDispose(cancel)
  return { tokens, loading, error, approximate }
}
