// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createOfficeThumbnails } from './officeThumbnails'

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['png'], { type: 'image/png' })))
})
afterEach(() => vi.restoreAllMocks())

function harness() {
  const canvases = new Set<HTMLCanvasElement>()
  const render = vi.fn(async (canvas: HTMLCanvasElement, _index: number, width: number) => {
    canvases.add(canvas); canvas.width = width; canvas.height = Math.round(width * 4 / 3)
  })
  const getPageSize = vi.fn((index: number) => index < 100 ? { width: 600, height: 800 } : null)
  const thumbnails = createOfficeThumbnails({ document, getPageSize, render })
  return { thumbnails, render, canvases, getPageSize }
}

describe('bounded Office thumbnails', () => {
  it('reuses one canvas and an LRU Blob cache without owning object URLs', async () => {
    const h = harness()
    const first = await h.thumbnails.request(0)
    expect(first).toMatchObject({ width: 160, height: 213, blob: expect.any(Blob) })
    expect(await h.thumbnails.request(0)).toBe(first)
    expect(h.render).toHaveBeenCalledTimes(1)
    for (let index = 1; index <= 24; index++) await h.thumbnails.request(index)
    await h.thumbnails.request(0)
    expect(h.render).toHaveBeenCalledTimes(26)
    expect(h.canvases.size).toBe(1)
    expect([...h.canvases][0]!.width).toBe(0)
    await h.thumbnails.destroy()
  })

  it('serializes rendering and drops queued or active cancelled requests', async () => {
    const h = harness()
    let finish!: () => void
    h.render.mockImplementationOnce((canvas, _index, width) => {
      h.canvases.add(canvas); canvas.width = width; canvas.height = 213
      return new Promise(resolve => { finish = resolve })
    })
    const active = new AbortController(), queued = new AbortController()
    const first = h.thumbnails.request(0, active.signal).catch(error => error)
    const second = h.thumbnails.request(1, queued.signal).catch(error => error)
    const third = h.thumbnails.request(2)
    expect(h.render).toHaveBeenCalledTimes(1)
    queued.abort(); active.abort()
    expect(await first).toMatchObject({ name: 'AbortError' })
    expect(await second).toMatchObject({ name: 'AbortError' })
    finish(); await third
    expect(h.render.mock.calls.map(call => call[1])).toEqual([0, 2])
    await h.thumbnails.destroy()
  })

  it('bounds the pending queue and waits for the sole active renderer before releasing its owner', async () => {
    const h = harness()
    let finish!: () => void
    h.render.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const requests = Array.from({ length: 26 }, (_, index) => h.thumbnails.request(index).catch(error => error))
    expect(await requests[25]).toMatchObject({ name: 'AbortError' })
    const released = vi.fn(), settling = h.thumbnails.destroy()!.then(released)
    expect(released).not.toHaveBeenCalled()
    expect(h.render).toHaveBeenCalledTimes(1)
    finish(); await settling
    expect(released).toHaveBeenCalledTimes(1)
    expect((await Promise.all(requests)).every(result => result.name === 'AbortError')).toBe(true)
    await expect(h.thumbnails.request(0)).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('limits encoded cache bytes independently of the page count', async () => {
    vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation(callback => callback(new Blob([new Uint8Array(1_500_000)])))
    const h = harness()
    await h.thumbnails.request(0); await h.thumbnails.request(1); await h.thumbnails.request(2)
    await h.thumbnails.request(0)
    expect(h.render).toHaveBeenCalledTimes(4)
    await h.thumbnails.destroy()
  })

  it('rejects invalid indices and extreme page proportions before rendering', async () => {
    const h = harness()
    await expect(h.thumbnails.request(-1)).rejects.toBeInstanceOf(RangeError)
    await expect(h.thumbnails.request(100)).rejects.toBeInstanceOf(RangeError)
    h.getPageSize.mockReturnValue({ width: 1, height: 1_000_000 })
    await expect(h.thumbnails.request(0)).rejects.toBeInstanceOf(RangeError)
    expect(h.render).not.toHaveBeenCalled()
    await h.thumbnails.destroy()
  })
})
