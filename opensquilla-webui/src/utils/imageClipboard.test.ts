// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  IMAGE_CLIPBOARD_MAX_BYTES,
  SVG_CLIPBOARD_MAX_BYTES,
  isClipboardImageCandidate,
  isSvgClipboardCandidate,
  prepareClipboardBlob,
  writePreparedClipboard,
} from './imageClipboard'

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"><rect width="12" height="8" fill="red"/></svg>'
let write: ReturnType<typeof vi.fn>
let supported: ReturnType<typeof vi.fn<(type: string) => boolean>>
let images: HTMLImageElement[]
let imageLoads: boolean
let imageSize: [number, number]
let urls: Blob[]
let revoke: ReturnType<typeof vi.spyOn>
let draw: ReturnType<typeof vi.fn>
let canvasWidth: number
let canvasHeight: number

class TestClipboardItem {
  static supports(type: string) { return supported(type) }
  constructor(readonly entries: Record<string, Promise<Blob>>) {}
}

beforeEach(() => {
  supported = vi.fn((_type: string) => true)
  write = vi.fn(async (items: TestClipboardItem[]) => { await Promise.all(Object.values(items[0]!.entries)) })
  vi.stubGlobal('ClipboardItem', TestClipboardItem)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write } })
  images = []
  urls = []
  imageLoads = true
  imageSize = [12, 8]
  canvasWidth = 0
  canvasHeight = 0
  draw = vi.fn()
  vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
    urls.push(blob as Blob)
    return `blob:fixture-${urls.length}`
  })
  revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.stubGlobal('Image', class {
    constructor() {
      const image = document.createElement('img')
      Object.defineProperties(image, {
        naturalWidth: { get: () => imageSize[0] },
        naturalHeight: { get: () => imageSize[1] },
        src: { set: () => { if (imageLoads) queueMicrotask(() => image.onload?.(new Event('load'))) } },
      })
      images.push(image)
      return image
    }
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (this: HTMLCanvasElement, callback) {
    canvasWidth = this.width
    canvasHeight = this.height
    callback(new Blob(['encoded'], { type: 'image/png' }))
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('image clipboard preparation', () => {
  it('starts writing synchronously and passes deferred PNG preparation to ClipboardItem', async () => {
    let finish!: (value: Blob) => void
    const prepared = new Promise<Blob>(resolve => { finish = resolve })
    const prepare = vi.fn(() => prepared)
    const pending = writePreparedClipboard('image', prepare)
    expect(write).toHaveBeenCalledOnce()
    expect(prepare).not.toHaveBeenCalled()
    const item = write.mock.calls[0]![0][0] as TestClipboardItem
    expect(Object.keys(item.entries)).toEqual(['image/png'])
    await Promise.resolve()
    finish(new Blob(['png'], { type: 'image/png' }))
    await pending
  })

  it('does not start preparation when PNG is unsupported and permits older supports-less implementations', async () => {
    supported.mockReturnValue(false)
    const prepare = vi.fn(async () => new Blob(['png'], { type: 'image/png' }))
    await expect(writePreparedClipboard('image', prepare)).rejects.toMatchObject({ kind: 'unsupported' })
    expect(prepare).not.toHaveBeenCalled()
    Object.defineProperty(TestClipboardItem, 'supports', { configurable: true, value: undefined })
    await writePreparedClipboard('image', prepare)
    Object.defineProperty(TestClipboardItem, 'supports', { configurable: true, value: (type: string) => supported(type) })
  })

  it.each(['', 'application/octet-stream', 'text/plain', 'text/xml', 'application/xml'])('allows SVG fallback MIME %j only for .svg', type => {
    expect(isSvgClipboardCandidate({ name: 'drawing.SVG', mime: type })).toBe(true)
    expect(isSvgClipboardCandidate({ name: 'drawing.txt', mime: type })).toBe(false)
    expect(isClipboardImageCandidate({ name: 'drawing.SVG', mime: type })).toBe(true)
  })

  it('rejects conflicting explicit types even when the filename claims SVG', async () => {
    expect(isSvgClipboardCandidate({ name: 'drawing.svg', mime: 'text/html' })).toBe(false)
    expect(isClipboardImageCandidate({ name: 'drawing.svg', mime: 'text/html' })).toBe(false)
    await expect(prepareClipboardBlob(new Blob([SVG], { type: 'text/html' }),
      { name: 'drawing.svg', mime: 'image/svg+xml' }, 'image', new AbortController().signal))
      .rejects.toMatchObject({ kind: 'invalidSvg' })
    expect(images).toHaveLength(0)
  })

  it('copies exact UTF-8 source, including BOM, without image decoding or DOM insertion', async () => {
    const text = `\uFEFF<?xml version="1.0"?>\n${SVG.replace('</svg>', '<text>图🙂</text></svg>')}\r\n`
    const source = new Blob([text], { type: 'text/plain' })
    const result = await prepareClipboardBlob(source, { name: 'drawing.svg', mime: 'text/plain' },
      'svg-source', new AbortController().signal)
    expect(result.type).toBe('text/plain')
    expect(new Uint8Array(await result.arrayBuffer())).toEqual(new Uint8Array(await source.arrayBuffer()))
    expect(images).toHaveLength(0)
    expect(document.querySelector('svg')).toBeNull()
  })

  it.each([
    new Blob([new Uint8Array([255, 254])], { type: 'image/svg+xml' }),
    new Blob(['<html><body>no</body></html>'], { type: 'image/svg+xml' }),
    new Blob(['<svg xmlns="http://www.w3.org/2000/svg">\0</svg>'], { type: 'image/svg+xml' }),
    new Blob(['<svg xmlns="https://invalid.example/svg"/>'], { type: 'image/svg+xml' }),
  ])('rejects invalid SVG source', async blob => {
    await expect(prepareClipboardBlob(blob, { name: 'drawing.svg', mime: 'image/svg+xml' },
      'svg-source', new AbortController().signal)).rejects.toMatchObject({ kind: 'invalidSvg' })
  })

  it.each([
    ['image', IMAGE_CLIPBOARD_MAX_BYTES], ['svg-source', SVG_CLIPBOARD_MAX_BYTES],
  ] as const)('enforces byte limits before decoding for %s', async (mode, maxBytes) => {
    const blob = new Blob([SVG], { type: 'image/svg+xml' })
    Object.defineProperty(blob, 'size', { value: maxBytes + 1 })
    await expect(prepareClipboardBlob(blob, { name: 'drawing.svg' }, mode,
      new AbortController().signal)).rejects.toMatchObject({ kind: 'tooLarge' })
    expect(images).toHaveLength(0)
  })

  it.each([
    '<svg xmlns="http://www.w3.org/2000/svg" width="8193" height="1"/>',
    '<svg xmlns="http://www.w3.org/2000/svg" width="5000" height="5000"/>',
  ])('rejects oversized SVG dimensions before allocating an image', async text => {
    await expect(prepareClipboardBlob(new Blob([text], { type: 'image/svg+xml' }),
      { name: 'drawing.svg' }, 'image', new AbortController().signal)).rejects.toMatchObject({ kind: 'tooLarge' })
    expect(images).toHaveLength(0)
  })

  it('rejects SVGs without usable dimensions and rasterizes viewBox-only SVGs explicitly', async () => {
    await expect(prepareClipboardBlob(new Blob(['<svg xmlns="http://www.w3.org/2000/svg"/>']),
      { name: 'drawing.svg' }, 'image', new AbortController().signal)).rejects.toMatchObject({ kind: 'invalidSvg' })
    const result = await prepareClipboardBlob(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 8"/>']),
      { name: 'drawing.svg' }, 'image', new AbortController().signal)
    expect(result.type).toBe('image/png')
    expect(await urls[0]!.text()).toContain('width="12"')
    expect(await urls[0]!.text()).toContain('height="8"')
    expect([canvasWidth, canvasHeight]).toEqual([12, 8])
    expect(document.querySelector('svg, img, canvas')).toBeNull()
    expect(revoke).toHaveBeenCalledWith('blob:fixture-1')
  })

  it('rejects oversized raster dimensions before canvas rendering', async () => {
    imageSize = [5000, 5000]
    await expect(prepareClipboardBlob(new Blob(['png'], { type: 'image/png' }),
      { name: 'drawing.png', mime: 'image/png' }, 'image', new AbortController().signal))
      .rejects.toMatchObject({ kind: 'tooLarge' })
    expect(draw).not.toHaveBeenCalled()
    expect(revoke).toHaveBeenCalledOnce()
  })

  it('revokes image resources when cancelled during image loading', async () => {
    imageLoads = false
    const controller = new AbortController()
    const pending = prepareClipboardBlob(new Blob(['png'], { type: 'image/png' }),
      { name: 'drawing.png', mime: 'image/png' }, 'image', controller.signal)
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(revoke).toHaveBeenCalledOnce()
    expect(draw).not.toHaveBeenCalled()
  })

  it('reports encoder failure and releases the image', async () => {
    vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation(callback => callback(null))
    await expect(prepareClipboardBlob(new Blob(['png'], { type: 'image/png' }),
      { name: 'drawing.png' }, 'image', new AbortController().signal)).rejects.toMatchObject({ kind: 'invalidImage' })
    expect(revoke).toHaveBeenCalledOnce()
  })
})
