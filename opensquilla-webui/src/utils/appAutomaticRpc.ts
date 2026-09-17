import { createCoalescedRefresh } from './coalescedRefresh'

interface AppAutomaticRpcOptions {
  available: () => boolean
  admitted: () => boolean
  resumeDirectory: () => Promise<void>
  subscribeCron: () => void
  loadAgents: () => Promise<unknown>
  loadSidebar: () => Promise<void>
  cancelSidebar: () => void
}

/** App-owned background reads start only on an admitted, ready connection. */
export function createAppAutomaticRpc(options: AppAutomaticRpcOptions) {
  let mounted = false
  let disposed = false
  let started = false
  let generation = 0
  let directoryReady = false
  const admitted = () => mounted && !disposed && options.available() && options.admitted()
  const allowed = () => admitted() && directoryReady
  const sidebar = createCoalescedRefresh({
    run: options.loadSidebar,
    allowed,
    delayMs: 150,
  })

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
    directoryReady = false
    if (disposed) return
    if (!options.available()) options.cancelSidebar()
    sidebar.defer()
    return resume()
  }

  function admissionChanged() {
    generation++
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
    sidebar.dispose()
    options.cancelSidebar()
  }

  return { mount, load, schedule: sidebar.schedule, availabilityChanged, admissionChanged, dispose }
}
