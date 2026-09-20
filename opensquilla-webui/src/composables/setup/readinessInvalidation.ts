// Settings saves hot-apply config without a server push. Consumers refresh
// their readiness snapshots and config-dependent activity through this signal.

type ReadinessListener = () => void

const readinessListeners = new Set<ReadinessListener>()

/** Subscribe to readiness invalidations; returns an unsubscribe function. */
export function onReadinessInvalidated(listener: ReadinessListener): () => void {
  readinessListeners.add(listener)
  return () => { readinessListeners.delete(listener) }
}

/** Signal that gateway config changed and readiness snapshots must re-fetch. */
export function invalidateReadiness(): void {
  for (const listener of Array.from(readinessListeners)) listener()
}
