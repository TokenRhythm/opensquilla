import { createCoalescedRefresh } from './coalescedRefresh'
import type { SessionListLoadResult } from '@/composables/useSessions'

const SIDEBAR_RETRY_DELAYS_MS = [500, 1_500, 3_000]

interface AppAutomaticRpcOptions {
  available: () => boolean
  admitted: () => boolean
  resumeDirectory: () => Promise<void>
  subscribeCron: () => void
  loadAgents: () => Promise<unknown>
  loadSidebar: () => Promise<SessionListLoadResult>
  cancelSidebar: () => void
}

/** App-owned background reads start only on an admitted, ready connection. */
export function createAppAutomaticRpc(options: AppAutomaticRpcOptions) {
  let mounted = false
  let disposed = false
  let started = false
  let generation = 0
  let directoryReady = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let retryAttempt = 0
  let refreshing = false
  const admitted = () => mounted && !disposed && options.available() && options.admitted()
  const allowed = () => admitted() && directoryReady
  const sidebar = createCoalescedRefresh({
    run: refreshSidebar,
    allowed,
    delayMs: 150,
  })

  function clearRetry(resetAttempts = false) {
    if (retryTimer !== null) clearTimeout(retryTimer)
    retryTimer = null
    if (resetAttempts) retryAttempt = 0
  }

  async function refreshSidebar() {
    clearRetry()
    const current = generation
    refreshing = true
    let result: SessionListLoadResult
    try {
      result = await options.loadSidebar()
    } finally {
      refreshing = false
    }
    if (current !== generation || disposed || result === 'superseded') return
    if (result === 'applied') {
      clearRetry(true)
      return
    }
    const delay = SIDEBAR_RETRY_DELAYS_MS[retryAttempt]
    if (delay === undefined) return
    retryAttempt++
    retryTimer = setTimeout(() => {
      retryTimer = null
      if (current !== generation || disposed) return
      // Defer while chat owns admission; its release will flush the dirty read.
      sidebar.defer()
      sidebar.flush()
    }, delay)
  }

  function schedule() {
    if (disposed) return
    clearRetry(true)
    sidebar.schedule()
  }

  function foreground() {
    // Focus and visibility events may both fire for a single return to App.
    // Use the same coalescing and admission gate as directory invalidations.
    schedule()
  }

  async function resume() {
    if (!admitted()) return
    const current = ++generation
    directoryReady = false
    options.subscribeCron()
    // Bind the live-only lease before refreshing the snapshot. A pending bind
    // from an older connection or a disposed App must never start a new read.
    await options.resumeDirectory()
    if (current !== generation || !admitted()) return
    directoryReady = true
    if (!started) {
      started = true
      void options.loadAgents()
      sidebar.defer()
    }
    sidebar.flush()
  }

  function load(): Promise<void> {
    if (disposed) return Promise.resolve()
    clearRetry(true)
    // CoalescedRefresh.load intentionally bypasses admission for its other
    // callers; both direct App refreshes and scheduled work need this gate.
    if (!allowed()) {
      sidebar.defer()
      return Promise.resolve()
    }
    return sidebar.load()
  }

  function availabilityChanged() {
    generation++
    clearRetry(true)
    directoryReady = false
    if (disposed) return
    if (!options.available()) options.cancelSidebar()
    sidebar.defer()
    return resume()
  }

  function admissionChanged() {
    generation++
    // Preserve a read interrupted by chat bootstrap or an outstanding failure,
    // without refetching a clean directory on every admission transition.
    if (refreshing || retryAttempt > 0) sidebar.defer()
    clearRetry(true)
    directoryReady = false
    return resume()
  }

  function mount() {
    if (disposed) return
    mounted = true
    return resume()
  }

  function dispose() {
    disposed = true
    mounted = false
    generation++
    directoryReady = false
    clearRetry(true)
    sidebar.dispose()
    options.cancelSidebar()
  }

  return { mount, load, schedule, foreground, availabilityChanged, admissionChanged, dispose }
}
