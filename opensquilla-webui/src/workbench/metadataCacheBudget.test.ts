import { describe, expect, it, vi } from 'vitest'
import { MetadataCacheBudget } from './metadataCacheBudget'

describe('metadata cache budget', () => {
  it('uses count and bytes, evicts LRU metadata, and never drops leased work', () => {
    const cache = new MetadataCacheBudget(2, 100)
    const release = cache.retain('open')
    cache.set('open', 'x'.repeat(100))
    cache.set('cold', 'small')
    const evict = vi.fn()
    cache.prune(() => false, evict)
    expect(evict).toHaveBeenCalledWith('cold')
    expect(cache.size).toBe(1)
    expect(cache.estimatedBytes).toBeGreaterThan(100)
    release(); release()
    cache.prune(() => false, evict)
    expect(evict).toHaveBeenCalledWith('open')
    expect(cache.estimatedBytes).toBe(0)
  })
})
