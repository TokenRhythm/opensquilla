// Cache source + palette together: switching themes must never return the
// previous theme's pixels. The byte bound also includes the retained source.
export class DiagramCache {
  private entries = new Map<string, string>()
  private bytes = 0

  constructor(private readonly maxBytes = 4 * 1024 * 1024, private readonly maxEntries = 24) {}

  get(key: string): string | undefined {
    const value = this.entries.get(key)
    if (value !== undefined) {
      this.entries.delete(key)
      this.entries.set(key, value)
    }
    return value
  }

  set(key: string, value: string): void {
    const size = 2 * (key.length + value.length)
    if (size > this.maxBytes) return
    const previous = this.entries.get(key)
    if (previous !== undefined) {
      this.bytes -= 2 * (key.length + previous.length)
      this.entries.delete(key)
    }
    while (this.entries.size >= this.maxEntries || this.bytes + size > this.maxBytes) {
      const oldest = this.entries.entries().next().value
      if (!oldest) break
      this.entries.delete(oldest[0])
      this.bytes -= 2 * (oldest[0].length + oldest[1].length)
    }
    this.entries.set(key, value)
    this.bytes += size
  }
}
