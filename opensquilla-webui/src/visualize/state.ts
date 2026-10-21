import { createVisualizeSnapshotStore, type VisualizeSnapshotRevision } from './snapshotStore'

// New revisions may inherit controls, but an older revision cannot replace a
// newer snapshot even if the separate display-version index has been evicted.
const MAX_STATE = 16 * 1024
const snapshots = createVisualizeSnapshotStore<unknown>({
  prefix: 'opensquilla.visualize.v2.', legacyPrefix: 'opensquilla.visualize.v1.', maxValueLength: MAX_STATE,
  normalize(value) {
    const encoded = JSON.stringify(value)
    if (!encoded || encoded.length > MAX_STATE) throw new Error('Canvas state exceeds its JSON size limit')
    return JSON.parse(encoded)
  },
})

export function hasNewerVisualizeState(key: string, revision: VisualizeSnapshotRevision): boolean {
  return snapshots.hasNewer(key, revision)
}

export function readVisualizeState(key: string, revision?: VisualizeSnapshotRevision): unknown {
  return snapshots.read(key, revision)
}

export function saveVisualizeState(key: string, state: unknown, revision?: VisualizeSnapshotRevision): void {
  try { snapshots.save(key, state, revision) } catch { /* Ignore invalid or oversized guest state. */ }
}
