// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NativeWorkbenchApi } from '@/platform/types'
import { createNativeTabSnapshots } from './nativeTabSnapshots'
const encoded = vi.hoisted(() => ({ create: vi.fn() }))
vi.mock('./browserThumbnail', () => ({ createBrowserThumbnail: encoded.create }))
beforeEach(() => { encoded.create.mockReset().mockResolvedValue('blob:cached-native'); vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {}) })
afterEach(() => vi.restoreAllMocks())
function native() { return { getWorkbenchBrowserTarget: vi.fn().mockResolvedValue({ targetRef: 'bound-target' }), captureWorkbenchScreenshot: vi.fn().mockResolvedValue({ dataBase64: '', width: 900, height: 700 }) } }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
describe('completed native artifact snapshots', () => {
  it('captures a completed page once, reads cached output without work, and clears it on navigation', async () => {
    const api = native(), changed = vi.fn(), cache = createNativeTabSnapshots(api as unknown as NativeWorkbenchApi, changed)
    expect(cache.get('page', 'r1')).toBeUndefined(); expect(api.getWorkbenchBrowserTarget).not.toHaveBeenCalled()
    await cache.ready('page', 'r1'); await cache.ready('page', 'r1')
    expect(api.captureWorkbenchScreenshot).toHaveBeenCalledExactlyOnceWith({ surfaceId: 'page', targetRef: 'bound-target' })
    expect(cache.get('page', 'r1')).toBe('blob:cached-native')
    cache.invalidate('page'); expect(cache.get('page', 'r1')).toBeUndefined()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:cached-native')
    cache.clear()
  })
  it('rejects a late snapshot after navigation or close even when the same tab ID reopens', async () => {
    const api = native(), old = deferred<string | null>()
    encoded.create.mockReturnValueOnce(old.promise).mockResolvedValueOnce('blob:new-page')
    const cache = createNativeTabSnapshots(api as unknown as NativeWorkbenchApi, vi.fn())
    const first = cache.ready('page', 'r1')
    for (let i = 0; i < 5; i++) await Promise.resolve()
    cache.prune(new Set()); await cache.ready('page', 'r2')
    old.resolve('blob:old-page'); await first
    expect(cache.get('page', 'r2')).toBe('blob:new-page')
    expect(cache.get('page', 'r1')).toBeUndefined()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:old-page')
    cache.clear()
  })
  it('keeps one previous frame for occlusion during reload while withholding stale hover content', async () => {
    const api = native(), cache = createNativeTabSnapshots(api as unknown as NativeWorkbenchApi, vi.fn())
    encoded.create.mockResolvedValueOnce('blob:first-frame').mockResolvedValueOnce('blob:second-frame')
    await cache.ready('page', 'r1')
    cache.invalidate('page', true)
    expect(cache.get('page', 'r1')).toBeUndefined()
    expect(cache.snapshot('page', 'r1')).toBe('blob:first-frame')
    expect(URL.revokeObjectURL).not.toHaveBeenCalledWith('blob:first-frame')
    await cache.ready('page', 'r1')
    expect(cache.get('page', 'r1')).toBe('blob:second-frame')
    expect(cache.snapshot('page', 'r1')).toBe('blob:second-frame')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first-frame')
    expect(api.captureWorkbenchScreenshot).toHaveBeenCalledTimes(2)
    cache.invalidate('page')
    expect(cache.snapshot('page', 'r1')).toBeUndefined()
    cache.clear()
  })
  it('uses metadata fallback for unavailable capture and releases a capture completing after teardown', async () => {
    const absent = createNativeTabSnapshots(undefined, vi.fn())
    await expect(absent.ready('page', 'r')).resolves.toBeUndefined()
    const api = native(), done = deferred<string | null>(), cache = createNativeTabSnapshots(api as unknown as NativeWorkbenchApi, vi.fn())
    encoded.create.mockReturnValueOnce(done.promise)
    const attempt = cache.ready('page', 'r')
    for (let i = 0; i < 5; i++) await Promise.resolve()
    cache.clear(); done.resolve('blob:too-late'); await attempt
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:too-late')
    expect(cache.get('page', 'r')).toBeUndefined()
  })
})
