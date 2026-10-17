/** LRU for reconstructible metadata only. Owner leases and writes are never evicted. */
export class MetadataCacheBudget {
  private readonly entries = new Map<string, number>()
  private readonly owners = new Map<string, number>()
  private bytes = 0

  constructor(private readonly maximumEntries: number, private readonly maximumBytes: number) {}

  touch(key: string) {
    const cost = this.entries.get(key)
    if (cost === undefined) return
    this.entries.delete(key)
    this.entries.set(key, cost)
  }

  set(key: string, value: unknown) {
    this.remove(key)
    // UTF-16 string size is an estimate of the JSON metadata, not JS heap.
    const cost = JSON.stringify(value).length * 2
    this.entries.set(key, cost)
    this.bytes += cost
  }

  retain(key: string): () => void {
    this.owners.set(key, (this.owners.get(key) ?? 0) + 1)
    let retained = true
    return () => {
      if (!retained) return
      retained = false
      const count = (this.owners.get(key) ?? 1) - 1
      if (count > 0) this.owners.set(key, count)
      else this.owners.delete(key)
    }
  }

  prune(protectedKey: (key: string) => boolean, evict: (key: string) => void) {
    for (const key of this.entries.keys()) {
      if (this.entries.size <= this.maximumEntries && this.bytes <= this.maximumBytes) break
      if (this.owners.has(key) || protectedKey(key)) continue
      this.remove(key)
      evict(key)
    }
  }

  remove(key: string) {
    this.bytes -= this.entries.get(key) ?? 0
    this.entries.delete(key)
  }

  clear() { this.entries.clear(); this.bytes = 0 }

  get size() { return this.entries.size }
  get estimatedBytes() { return this.bytes }
}
