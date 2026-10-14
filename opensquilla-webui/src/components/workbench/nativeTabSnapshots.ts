import type { NativeWorkbenchApi } from '@/platform/types'
import { createTabThumbnailCache } from '@/workbench/tabThumbnailCache'
import { createBrowserThumbnail } from './browserThumbnail'

/** Artifact pages already completed a native load. This cache never navigates,
 * never runs on hover, and rejects results from a replaced/closed document. */
export function createNativeTabSnapshots(native: NativeWorkbenchApi | undefined, changed: () => void) {
  const cache = createTabThumbnailCache()
  const generations = new Map<string, number>()
  const pending = new Map<string, number>()
  const stale = new Set<string>()
  let sequence = 0
  let disposed = false
  function invalidate(id: string, retainSnapshot = false) {
    generations.set(id, ++sequence)
    stale.add(id)
    if (!retainSnapshot) cache.remove(id)
    changed()
  }
  return {
    get: (id: string, fingerprint: string) => stale.has(id) ? undefined : cache.get(id, fingerprint),
    // The last completed frame only covers a temporarily occluded native surface.
    // It is not advertised as a current-page hover preview during navigation.
    snapshot: cache.get,
    invalidate,
    async ready(id: string, fingerprint: string) {
      if (disposed || (!stale.has(id) && cache.get(id, fingerprint))
        || !native?.getWorkbenchBrowserTarget || !native.captureWorkbenchScreenshot) return
      const generation = generations.get(id) ?? ++sequence
      generations.set(id, generation)
      if (pending.get(id) === generation) return
      const current = () => !disposed && generations.get(id) === generation
      pending.set(id, generation)
      try {
        const target = await native.getWorkbenchBrowserTarget({ surfaceId: id })
        if (!current() || !target.targetRef) return
        const screenshot = await native.captureWorkbenchScreenshot({ surfaceId: id, targetRef: target.targetRef })
        if (!current()) return
        const url = await createBrowserThumbnail(screenshot)
        if (!url) return
        if (!current()) { URL.revokeObjectURL(url); return }
        cache.adoptNative(id, fingerprint, url)
        stale.delete(id)
        changed()
      } catch { /* Optional snapshots do not change page success/error state. */ }
      finally { if (pending.get(id) === generation) pending.delete(id) }
    },
    prune(ids: ReadonlySet<string>) {
      for (const id of generations.keys()) if (!ids.has(id)) generations.delete(id)
      for (const id of stale) if (!ids.has(id)) stale.delete(id)
      cache.prune(ids)
    },
    clear() { disposed = true; generations.clear(); stale.clear(); cache.clear() },
  }
}
