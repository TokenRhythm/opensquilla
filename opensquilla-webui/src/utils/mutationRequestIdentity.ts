let fallbackSequence = 0

function privateLogicalKeyDigest(value: string): string {
  const bytes = new TextEncoder().encode(value)
  const digest = (seed: bigint) => {
    let hash = seed
    for (const byte of bytes) {
      hash ^= BigInt(byte)
      hash = BigInt.asUintN(64, hash * 0x100000001b3n)
    }
    return hash.toString(16).padStart(16, '0')
  }
  return `${digest(0xcbf29ce484222325n)}${digest(0x84222325cbf29ce4n)}`
}

/**
 * Creates an opaque request identity. Logical mutation content is never
 * embedded in the RPC value or retained in the pending-request registry.
 */
export function createMutationClientRequestId(prefix: string): string {
  const randomUuid = globalThis.crypto?.randomUUID?.()
  if (randomUuid) return `${prefix}-${randomUuid}`
  fallbackSequence += 1
  return `${prefix}-${Date.now()}-${fallbackSequence}`
}

/**
 * Retains a small LRU of ambiguous writes so an exact retry reuses its server
 * idempotency key. Different logical mutations always receive a fresh opaque
 * ID, even when they share the same document revision and source offsets.
 */
export class PendingMutationRequestIds {
  private readonly entries = new Map<string, {
    requestId: string
    state: 'ready' | 'pending'
    payloadJson: string | null
  }>()

  constructor(private readonly capacity = 32) {}

  idFor(logicalKey: string, prefix: string, preferredRequestId?: string): string {
    const key = privateLogicalKeyDigest(logicalKey)
    const existing = this.entries.get(key)
    if (existing) {
      this.entries.delete(key)
      this.entries.set(key, existing)
      return existing.requestId
    }

    const requestId = preferredRequestId || createMutationClientRequestId(prefix)
    while (this.entries.size >= Math.max(1, this.capacity)) {
      const oldestReady = [...this.entries].find(([, entry]) => entry.state === 'ready')?.[0]
      if (!oldestReady) {
        throw new Error('Too many page updates are waiting for confirmation.')
      }
      this.entries.delete(oldestReady)
    }
    this.entries.set(key, { requestId, state: 'ready', payloadJson: null })
    return requestId
  }

  freeze<T extends Readonly<Record<string, unknown>>>(
    logicalKey: string,
    requestId: string,
    payload: T,
  ): T {
    const key = privateLogicalKeyDigest(logicalKey)
    const entry = this.entries.get(key)
    if (!entry || entry.requestId !== requestId) {
      throw new Error('This page update can no longer be retried safely.')
    }
    const payloadJson = JSON.stringify(payload)
    if (entry.payloadJson !== null && entry.payloadJson !== payloadJson) {
      throw new Error('This page update no longer matches the pending request.')
    }
    entry.payloadJson = payloadJson
    return Object.freeze(JSON.parse(payloadJson)) as T
  }

  pendingPayload<T extends Readonly<Record<string, unknown>>>(
    logicalKey: string,
    requestId: string,
  ): T | null {
    const entry = this.entries.get(privateLogicalKeyDigest(logicalKey))
    if (
      !entry
      || entry.requestId !== requestId
      || entry.state !== 'pending'
      || entry.payloadJson === null
    ) return null
    return Object.freeze(JSON.parse(entry.payloadJson)) as T
  }

  markPending(logicalKey: string, requestId: string): void {
    const entry = this.entries.get(privateLogicalKeyDigest(logicalKey))
    if (entry?.requestId === requestId) entry.state = 'pending'
  }

  isPending(logicalKey: string, requestId: string): boolean {
    const entry = this.entries.get(privateLogicalKeyDigest(logicalKey))
    return entry?.requestId === requestId && entry.state === 'pending'
  }

  markNotApplied(logicalKey: string, requestId: string): void {
    this.release(logicalKey, requestId)
  }

  release(logicalKey: string, requestId: string): void {
    const key = privateLogicalKeyDigest(logicalKey)
    if (this.entries.get(key)?.requestId === requestId) this.entries.delete(key)
  }

  clear(): void {
    this.entries.clear()
  }
}
