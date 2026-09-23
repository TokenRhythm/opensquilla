import type { Attachment, ChatPendingItem } from '@/types/chat'

export const PARKED_PENDING_QUEUE_LIMITS = Object.freeze({
  sessions: 16,
  payloadBytes: 16 * 1024 * 1024,
  blobBytes: 32 * 1024 * 1024,
})

function snapshot<T>(value: T): T {
  if (!value || typeof value !== 'object' || value instanceof Blob) return value
  if (Array.isArray(value)) return value.map(snapshot) as T
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, snapshot(item)])) as T
}

function same(left: unknown, right: unknown): boolean {
  if (left === right) return true
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
    || left instanceof Blob || right instanceof Blob) return false
  const entries = Object.entries(left)
  return entries.length === Object.keys(right).length && entries.every(([key, value]) => (
    Object.prototype.hasOwnProperty.call(right, key) && same(value, (right as Record<string, unknown>)[key])
  ))
}

function persistentFields(item: ChatPendingItem): Record<string, unknown> {
  // UI identity and delivery leases are checked by isPinned, not part of WAL
  // payload equality. Clearing a lease must make a committed row evictable again.
  const keys: (keyof ChatPendingItem)[] = ['text', 'intent', 'attachments', 'pageContext',
    'selectedSkills', 'draftIds', 'confirmedPlainText', 'ownerSessionKey', 'ownerRequestId',
    'pendingInputId', 'pendingClientRequestId', 'pendingClientMessageId', 'pendingPersistenceState',
    'pendingMayHaveServerCopy', 'pendingDeliveryIdentity', 'pendingRetainAfterCancel',
    'pendingRequestFingerprint', 'pendingServerRevision', 'pendingPosition', 'pendingWalRevision',
    'pendingCreatedAt', 'retiredAnnotationInput']
  return Object.fromEntries(keys.filter(key => item[key] !== undefined).map(key => [key, item[key]]))
}

export class ParkedPendingQueueCache extends Map<string, ChatPendingItem[]> {
  private readonly committed = new WeakMap<ChatPendingItem, Record<string, unknown>>()
  private readonly ownedUrls = new Set<string>()
  private urlPruneQueued = false
  private readonly unwrapObject: <T extends object>(value: T) => T

  constructor(private readonly options: {
    isPinned: (item: ChatPendingItem) => boolean
    isUrlRetained: (url: string) => boolean
    /** Return a stable underlying object, never a copy. */
    unwrapObject?: <T extends object>(value: T) => T
  }) {
    super()
    this.unwrapObject = options.unwrapObject ?? (value => value)
  }

  snapshotForCommit(item: ChatPendingItem): Record<string, unknown> {
    return snapshot(persistentFields(item))
  }

  restoreAttachment(attachment: Attachment): Attachment {
    if (!attachment.dataUrl?.startsWith('blob:')) return attachment
    if (attachment.file) {
      attachment.dataUrl = URL.createObjectURL(attachment.file)
      this.ownedUrls.add(attachment.dataUrl)
      this.pruneUrlsAfterMutation()
    } else if (attachment.data) {
      attachment.dataUrl = `data:${attachment.mime};base64,${attachment.data}`
    }
    return attachment
  }

  private releaseUrl(url: string): void {
    this.ownedUrls.delete(url)
    try { URL.revokeObjectURL(url) } catch {}
  }

  private pruneUrlsAfterMutation(): void {
    if (this.urlPruneQueued) return
    this.urlPruneQueued = true
    // Hydration/handoff builds records before installing them in an array.
    // Prune after that synchronous mutation, including unused reconciliation rows.
    queueMicrotask(() => {
      this.urlPruneQueued = false
      for (const url of this.ownedUrls) {
        if (!this.options.isUrlRetained(url)) this.releaseUrl(url)
      }
    })
  }

  rememberCommitted(item: ChatPendingItem, committed: Record<string, unknown>): void {
    const key = this.unwrapObject(item)
    if (same(persistentFields(item), committed)) this.committed.set(key, committed)
    else this.committed.delete(key)
    this.trim()
  }

  private canEvict(items: ChatPendingItem[]): boolean {
    return items.every(item => {
      const saved = this.committed.get(this.unwrapObject(item))
      return Boolean(item.pendingInputId && saved && same(persistentFields(item), saved)
        && !this.options.isPinned(item)
        && item.attachments.every(attachment => (
          !attachment.dataUrl?.startsWith('blob:') || attachment.file || attachment.data
        )))
    })
  }

  private measure(items: ChatPendingItem[]) {
    let payloadBytes = 0
    let blobBytes = 0
    const seen = new WeakSet<object>()
    const count = (value: unknown): void => {
      if (typeof value === 'string') { payloadBytes += value.length * 2; return }
      if (!value || typeof value !== 'object') { payloadBytes += 8; return }
      const raw = this.unwrapObject(value)
      if (seen.has(raw)) return
      seen.add(raw)
      if (raw instanceof Blob) { blobBytes += raw.size; return }
      payloadBytes += 32
      for (const [key, item] of Object.entries(raw)) {
        payloadBytes += key.length * 2
        count(item)
      }
    }
    count(items)
    return { payloadBytes, blobBytes }
  }

  private entriesWithUsage() {
    return [...this].map(([sessionKey, items]) => ({
      sessionKey, items, ...this.measure(items), protected: !this.canEvict(items),
    }))
  }

  usage() {
    const usage = { sessions: this.size, payloadBytes: 0, blobBytes: 0,
      protectedSessions: 0, protectedPayloadBytes: 0, protectedBlobBytes: 0,
      reclaimableSessions: 0, reclaimablePayloadBytes: 0, reclaimableBlobBytes: 0 }
    // Count shared objects once within each session. Across sessions these are
    // conservative retention estimates, not a measurement of process RSS.
    for (const entry of this.entriesWithUsage()) {
      usage.payloadBytes += entry.payloadBytes
      usage.blobBytes += entry.blobBytes
      if (entry.protected) {
        usage.protectedSessions++
        usage.protectedPayloadBytes += entry.payloadBytes
        usage.protectedBlobBytes += entry.blobBytes
      } else {
        usage.reclaimableSessions++
        usage.reclaimablePayloadBytes += entry.payloadBytes
        usage.reclaimableBlobBytes += entry.blobBytes
      }
    }
    return usage
  }

  override set(sessionKey: string, items: ChatPendingItem[]): this {
    // Parking or updating a queue makes it the most recently retained session.
    super.delete(sessionKey)
    super.set(sessionKey, items)
    this.trim()
    return this
  }

  trim(): void {
    this.pruneUrlsAfterMutation()
    const entries = this.entriesWithUsage()
    let sessions = entries.length
    let payloadBytes = 0
    let blobBytes = 0
    for (const entry of entries) { payloadBytes += entry.payloadBytes; blobBytes += entry.blobBytes }
    for (const entry of entries) {
      if (sessions <= PARKED_PENDING_QUEUE_LIMITS.sessions
        && payloadBytes <= PARKED_PENDING_QUEUE_LIMITS.payloadBytes
        && blobBytes <= PARKED_PENDING_QUEUE_LIMITS.blobBytes) return
      // Memory budgets never authorize dropping a draft or an in-flight owner.
      if (entry.protected) continue
      super.delete(entry.sessionKey)
      sessions--
      payloadBytes -= entry.payloadBytes
      blobBytes -= entry.blobBytes
      const urls = new Set(entry.items.map(item => item.attachments).flat()
        .map(attachment => attachment.dataUrl).filter((url): url is string => Boolean(url?.startsWith('blob:'))))
      for (const url of urls) {
        if (!this.options.isUrlRetained(url)) {
          this.releaseUrl(url)
        }
      }
    }
  }

  dispose(): void {
    super.clear()
    for (const url of this.ownedUrls) this.releaseUrl(url)
  }
}
