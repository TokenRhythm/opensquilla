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
  let directoryAttemptSettled = false
  let directoryNeedsRetry = false
  let directoryRetryTimer: ReturnType<typeof setTimeout> | null = null
  let directoryRetryAttempt = 0
  let directoryWork: { generation: number, promise: Promise<void> } | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let retryAttempt = 0
  let refreshing = false
  const admitted = () => mounted && !disposed && options.available() && options.admitted()
  const allowed = () => admitted() && directoryAttemptSettled
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

  function clearDirectoryRetry(resetAttempts = false) {
    if (directoryRetryTimer !== null) clearTimeout(directoryRetryTimer)
    directoryRetryTimer = null
    if (resetAttempts) directoryRetryAttempt = 0
  }

  function retryDirectory(current: number) {
    const delay = SIDEBAR_RETRY_DELAYS_MS[directoryRetryAttempt]
    if (delay === undefined) return
    directoryRetryAttempt++
    directoryRetryTimer = setTimeout(() => {
      directoryRetryTimer = null
      if (current === generation && admitted()) void resume()
    }, delay)
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
    if (directoryNeedsRetry) {
      clearDirectoryRetry(true)
      void resume()
    }
  }

  function connectionHealthChanged(health: 'healthy' | 'suspect') {
    // A suspect transport can keep availability='available', so the existing
    // availability watcher does not flush a sidebar read when liveness returns.
    // Reuse the bounded, coalesced foreground path only on healthy recovery.
    if (health === 'healthy') foreground()
  }

  function resume(): Promise<void> | undefined {
    if (!admitted()) return
    const current = generation
    if (directoryWork?.generation === current) return directoryWork.promise
    options.subscribeCron()
    // Attempt the lease before the first read, but a failed live subscription
    // must not hide a usable snapshot. Rebinding later refreshes missed changes.
    const work = (async () => {
      try {
        await options.resumeDirectory()
        if (current !== generation || !admitted()) return
        if (directoryNeedsRetry) sidebar.defer()
        directoryNeedsRetry = false
        clearDirectoryRetry(true)
      } catch {
        if (current !== generation || !admitted()) return
        directoryNeedsRetry = true
        retryDirectory(current)
      }
      if (current !== generation || !admitted()) return
      directoryAttemptSettled = true
      if (!started) {
        started = true
        void options.loadAgents()
        sidebar.defer()
      }
      sidebar.flush()
    })().finally(() => {
      if (directoryWork?.promise === work) directoryWork = null
    })
    directoryWork = { generation: current, promise: work }
    return work
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
    clearDirectoryRetry(true)
    directoryAttemptSettled = false
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
    clearDirectoryRetry(true)
    directoryAttemptSettled = false
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
    directoryAttemptSettled = false
    clearRetry(true)
    clearDirectoryRetry(true)
    sidebar.dispose()
    options.cancelSidebar()
  }

  return {
    mount,
    load,
    schedule,
    foreground,
    connectionHealthChanged,
    availabilityChanged,
    admissionChanged,
    dispose,
  }
}
