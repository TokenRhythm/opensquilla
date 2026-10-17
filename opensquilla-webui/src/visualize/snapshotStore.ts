import { shallowRef } from 'vue'

/** Host-owned ownership metadata; never included in the value given to a guest. */
export interface VisualizeSnapshotRevision { version?: number; revisionId: string }

interface Snapshot<T> {
  schema: 2
  revision?: VisualizeSnapshotRevision
  value?: T
}

function revisionOf(value: unknown): VisualizeSnapshotRevision | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const source = value as Record<string, unknown>
  if (typeof source.revisionId !== 'string' || source.revisionId.length > 256 || !/^rev_[\w-]+$/.test(source.revisionId)) return undefined
  if (source.version !== undefined && (typeof source.version !== 'number' || !Number.isSafeInteger(source.version) || source.version < 1)) return undefined
  return { revisionId: source.revisionId, ...(source.version === undefined ? {} : { version: source.version as number }) }
}

function isOlder(owner: VisualizeSnapshotRevision | undefined, requested: VisualizeSnapshotRevision | undefined): boolean {
  if (owner?.version === undefined) return false
  if (requested?.version !== undefined) return requested.version < owner.version
  return requested?.revisionId !== owner.revisionId
}

/** Bounded snapshots retain their version fence for exactly as long as their data. */
export function createVisualizeSnapshotStore<T>(options: {
  prefix: string
  legacyPrefix: string
  maxValueLength: number
  normalize(value: unknown): T
}) {
  const memory = new Map<string, Snapshot<T>>()
  const legacySnapshots = new WeakSet<Snapshot<T>>()
  const ownerEpoch = shallowRef(0)
  const limit = 48
  const maxRecordLength = options.maxValueLength + 512
  const clone = (value: T): T => options.normalize(JSON.parse(JSON.stringify(value)))

  function remember(key: string, snapshot: Snapshot<T>): void {
    memory.delete(key); memory.set(key, snapshot)
    while (memory.size > limit) memory.delete(memory.keys().next().value!)
  }

  function sameOwner(left: VisualizeSnapshotRevision | undefined, right: VisualizeSnapshotRevision | undefined): boolean {
    return left?.revisionId === right?.revisionId && left?.version === right?.version
  }

  function persist(key: string, snapshot: Snapshot<T>): void {
    const ownerChanged = !sameOwner(memory.get(key)?.revision, snapshot.revision)
    remember(key, snapshot)
    if (ownerChanged) ownerEpoch.value += 1
    try {
      const encoded = JSON.stringify(snapshot)
      if (encoded.length > maxRecordLength) return
      sessionStorage.setItem(options.prefix + key, encoded)
      // Remove v1 only after v2 was saved; never revive a stale legacy value
      // later just because an unrelated display-version index was evicted.
      sessionStorage.removeItem(options.legacyPrefix + key)
      const keys = Object.keys(sessionStorage).filter(value => value.startsWith(options.prefix))
      for (const old of keys.filter(value => value !== options.prefix + key).slice(0, Math.max(0, keys.length - limit))) sessionStorage.removeItem(old)
    } catch { /* The bounded in-memory record still keeps both value and fence. */ }
  }

  function load(key: string): Snapshot<T> | undefined {
    const cached = memory.get(key)
    if (cached) return cached
    try {
      const encoded = sessionStorage.getItem(options.prefix + key)
      if (encoded !== null) {
        if (encoded.length > maxRecordLength) return undefined
        const value: unknown = JSON.parse(encoded)
        if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
        const candidate = value as Record<string, unknown>
        if (candidate.schema !== 2 || Object.keys(candidate).some(field => !['schema', 'revision', 'value'].includes(field))) return undefined
        const revision = revisionOf(candidate.revision)
        if (candidate.revision !== undefined && !revision) return undefined
        const snapshot: Snapshot<T> = { schema: 2, ...(revision ? { revision } : {}) }
        if (Object.prototype.hasOwnProperty.call(candidate, 'value')) snapshot.value = options.normalize(candidate.value)
        remember(key, snapshot)
        return snapshot
      }
      const legacy = sessionStorage.getItem(options.legacyPrefix + key)
      if (legacy === null || legacy.length > options.maxValueLength) return undefined
      const snapshot: Snapshot<T> = { schema: 2, value: options.normalize(JSON.parse(legacy)) }
      legacySnapshots.add(snapshot)
      remember(key, snapshot)
      return snapshot
    } catch { return undefined }
  }

  function ownerFor(previous: Snapshot<T> | undefined, revision: VisualizeSnapshotRevision | undefined): VisualizeSnapshotRevision | undefined {
    // An unversioned legacy receipt for the identical immutable revision must
    // not erase a known version number. Truly unversioned previews retain their
    // existing ordering; no monotonic ordering can be inferred from revision ids.
    if (previous?.revision?.version !== undefined && revision?.version === undefined) return previous.revision
    return revision ?? previous?.revision
  }

  return {
    hasNewer(key: string, revision: VisualizeSnapshotRevision): boolean {
      // Already-mounted standalone canvases must also become historical when
      // another revision takes ownership, even without the message-list index.
      void ownerEpoch.value
      const requested = revisionOf(revision)
      return !requested || isOlder(load(key)?.revision, requested)
    },
    read(key: string, revision?: VisualizeSnapshotRevision): T | undefined {
      const previous = load(key)
      const requested = revisionOf(revision)
      if (revision !== undefined && (!requested || isOlder(previous?.revision, requested))) return undefined
      // Revision-free reads preserve the old store API, returning only guest
      // data. Production callers supply their receipt to enforce ownership.
      const owner = ownerFor(previous, requested)
      if (!sameOwner(previous?.revision, owner) || (previous && legacySnapshots.has(previous))) {
        persist(key, { schema: 2, ...(owner ? { revision: owner } : {}),
          ...(previous && Object.prototype.hasOwnProperty.call(previous, 'value') ? { value: previous.value } : {}) })
      }
      return previous?.value === undefined ? undefined : clone(previous.value)
    },
    save(key: string, value: T, revision?: VisualizeSnapshotRevision): boolean {
      const previous = load(key)
      const requested = revisionOf(revision)
      if ((revision !== undefined && !requested) || isOlder(previous?.revision, requested)) return false
      const normalized = clone(value)
      const owner = ownerFor(previous, requested)
      persist(key, { schema: 2, ...(owner ? { revision: owner } : {}), value: normalized })
      return true
    },
  }
}
