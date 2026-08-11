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
  private readonly entries = new Map<string, string>()

  constructor(private readonly capacity = 32) {}

  idFor(logicalKey: string, prefix: string): string {
    const key = privateLogicalKeyDigest(logicalKey)
    const existing = this.entries.get(key)
    if (existing) {
      this.entries.delete(key)
      this.entries.set(key, existing)
      return existing
    }

    const requestId = createMutationClientRequestId(prefix)
    this.entries.set(key, requestId)
    while (this.entries.size > Math.max(1, this.capacity)) {
      const oldest = this.entries.keys().next().value
      if (typeof oldest !== 'string') break
      this.entries.delete(oldest)
    }
    return requestId
  }

  release(logicalKey: string, requestId: string): void {
    const key = privateLogicalKeyDigest(logicalKey)
    if (this.entries.get(key) === requestId) this.entries.delete(key)
  }

  clear(): void {
    this.entries.clear()
  }
}
