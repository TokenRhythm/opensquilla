export interface CoalescedRefresh {
  load: () => Promise<void>
  schedule: () => void
  defer: () => void
  flush: () => void
  dispose: () => void
}

interface CoalescedRefreshOptions {
  run: () => Promise<void>
  allowed: () => boolean
  delayMs: number
}

/**
 * Coalesce ordinary refreshes while preserving an event that arrives during
 * an in-flight read. The deferred refresh runs after that read settles.
 */
export function createCoalescedRefresh(options: CoalescedRefreshOptions): CoalescedRefresh {
  let timer: ReturnType<typeof setTimeout> | null = null
  let pending = false
  let inFlight: Promise<void> | null = null
  let disposed = false

  function flush() {
    if (disposed || !pending || !options.allowed() || inFlight) return
    pending = false
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    void load()
  }

  function load(): Promise<void> {
    if (inFlight) {
      pending = true
      return inFlight
    }
    let tracked: Promise<void>
    tracked = options.run().finally(() => {
      if (inFlight !== tracked) return
      inFlight = null
      if (pending && timer === null && !disposed) queueMicrotask(flush)
    })
    inFlight = tracked
    return tracked
  }

  function schedule() {
    if (disposed) return
    pending = true
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      flush()
    }, options.delayMs)
  }

  function defer() {
    if (!disposed) pending = true
  }

  function dispose() {
    disposed = true
    pending = false
    if (timer) clearTimeout(timer)
    timer = null
  }

  return { load, schedule, defer, flush, dispose }
}
