import type { PageAnnotationsSentDetail } from './promptAnnotations'

const SESSION_KEY_SEPARATOR = '\u0000'
const DEFAULT_MAX_ENTRIES = 32
const DEFAULT_TTL_MS = 5 * 60 * 1_000
const MAX_DRAFT_IDS = 64

interface PendingSendNotification {
  key: string
  sessionKeys: Set<string>
  draftIds: Set<string>
  expiresAt: number
}

export interface PageAnnotationSendQueueOptions {
  /** Prevent a disconnected Workbench from retaining events indefinitely. */
  maxEntries?: number
  /** How long an event may wait for its artifact item to mount. */
  ttlMs?: number
}

/**
 * Holds acceptance acknowledgements until the matching artifact preview is
 * mounted. The gateway response can race session materialization, so a fixed
 * number of nextTick retries is not a reliable handoff mechanism.
 */
export class PageAnnotationSendQueue {
  private readonly entries = new Map<string, PendingSendNotification>()
  private readonly maxEntries: number
  private readonly ttlMs: number

  constructor(options: PageAnnotationSendQueueOptions = {}) {
    this.maxEntries = Math.max(1, Math.floor(options.maxEntries ?? DEFAULT_MAX_ENTRIES))
    this.ttlMs = Math.max(1, Math.floor(options.ttlMs ?? DEFAULT_TTL_MS))
  }

  get size(): number {
    return this.entries.size
  }

  /**
   * Add an acknowledgement, merging events whose provisional/canonical
   * session identities overlap. This also makes duplicate response replay
   * idempotent while retaining IDs from two accepted sends in the same scope.
   */
  enqueue(detail: PageAnnotationsSentDetail, now = Date.now()): boolean {
    const sessionKeys = new Set(
      [detail.sessionKey, detail.requestSessionKey]
        .map(value => String(value || '').trim())
        .filter(Boolean),
    )
    const draftIds = new Set(
      (Array.isArray(detail.draftIds) ? detail.draftIds : [])
        .map(value => String(value || '').trim())
        .filter(Boolean)
        .slice(0, MAX_DRAFT_IDS),
    )
    if (sessionKeys.size === 0 || draftIds.size === 0) return false

    this.prune(now)
    const overlapping = [...this.entries.values()].filter(entry => (
      [...entry.sessionKeys].some(key => sessionKeys.has(key))
    ))
    // Preserve the first-seen order so activity rendering remains stable when
    // replayed responses are merged into one handoff.
    const mergedSessionKeys = new Set<string>()
    const mergedDraftIds = new Set<string>()
    let expiresAt = now + this.ttlMs
    for (const entry of overlapping) {
      for (const key of entry.sessionKeys) mergedSessionKeys.add(key)
      for (const id of entry.draftIds) mergedDraftIds.add(id)
      expiresAt = Math.max(expiresAt, entry.expiresAt)
      this.entries.delete(entry.key)
    }
    for (const key of sessionKeys) mergedSessionKeys.add(key)
    for (const id of draftIds) mergedDraftIds.add(id)
    const key = [...mergedSessionKeys].sort().join(SESSION_KEY_SEPARATOR)
    this.entries.set(key, {
      key,
      sessionKeys: mergedSessionKeys,
      draftIds: mergedDraftIds,
      expiresAt,
    })
    this.trimToLimit()
    return true
  }

  /**
   * Deliver all entries that match at least one currently mounted item.
   * A rejected/throwing delivery remains queued for the next lifecycle pass.
   */
  async flush<T>(
    items: readonly T[],
    sessionKeyForItem: (item: T) => string,
    deliver: (item: T, draftIds: readonly string[]) => Promise<boolean | void> | boolean | void,
    now = Date.now(),
  ): Promise<number> {
    this.prune(now)
    let delivered = 0
    for (const entry of [...this.entries.values()]) {
      if (this.entries.get(entry.key) !== entry) continue
      const matchingItems = items.filter(item => (
        entry.sessionKeys.has(String(sessionKeyForItem(item) || '').trim())
      ))
      if (matchingItems.length === 0) continue
      const ids = [...entry.draftIds]
      try {
        const results = await Promise.all(
          matchingItems.map(item => deliver(item, ids)),
        )
        if (results.some(result => result === false)) continue
      } catch {
        continue
      }
      // A replay can enqueue/merge the same identity while delivery awaits
      // the runtime queue. Never let this older snapshot delete the newer
      // entry; it will be delivered by the next lifecycle pass.
      if (this.entries.get(entry.key) !== entry) continue
      this.entries.delete(entry.key)
      delivered += 1
    }
    return delivered
  }

  clear(): void {
    this.entries.clear()
  }

  private prune(now: number): void {
    for (const [key, entry] of this.entries) {
      if (entry.expiresAt <= now) this.entries.delete(key)
    }
  }

  private trimToLimit(): void {
    while (this.entries.size > this.maxEntries) {
      const oldestKey = this.entries.keys().next().value
      if (typeof oldestKey !== 'string') return
      this.entries.delete(oldestKey)
    }
  }
}
