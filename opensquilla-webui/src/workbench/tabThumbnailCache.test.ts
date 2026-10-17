// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createTabThumbnailCache } from './tabThumbnailCache'
beforeEach(() => { let id = 0; vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:${++id}`); vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {}) })
afterEach(() => vi.restoreAllMocks())
describe('renderer-owned tab thumbnail cache', () => {
  it('does not reuse an old revision and releases URLs on replacement, close and teardown', () => {
    const cache = createTabThumbnailCache(), blob = new Blob(['image'], { type: 'image/png' })
    cache.save('file', 'r1', blob)
    expect(cache.get('file', 'r1')).toBe('blob:1'); expect(cache.get('file', 'r2')).toBeUndefined()
    cache.save('file', 'r2', blob); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:1')
    cache.save('second', 'r1', blob); cache.prune(new Set(['second']))
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:2')
    cache.clear(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:3')
  })
  it('bounds retained tab images and rejects oversized or executable content', () => {
    const cache = createTabThumbnailCache(), blob = new Blob(['image'], { type: 'image/png' })
    for (let i = 0; i < 17; i++) cache.save(`file-${i}`, 'r', blob)
    expect(cache.get('file-0', 'r')).toBeUndefined(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:1')
    cache.save('svg', 'r', new Blob(['<svg/>'], { type: 'image/svg+xml' }))
    cache.save('huge', 'r', new Blob([new Uint8Array(513 * 1024)], { type: 'image/png' }))
    expect(cache.get('svg', 'r')).toBeUndefined(); expect(cache.get('huge', 'r')).toBeUndefined()
    cache.clear()
  })
})
