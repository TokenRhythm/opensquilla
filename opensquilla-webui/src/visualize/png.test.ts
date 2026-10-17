// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { checkCaptureLayout, validateCapturePng, VISUALIZE_CAPTURE_MAX_BYTES } from './png'

// Decoder/pixel IO is mocked here; real opaque-iframe rasterization requires
// the separate browser acceptance check against the installed renderer.
function header(width = 2, height = 2) {
  const bytes = new Uint8Array(45)
  bytes.set([137, 80, 78, 71, 13, 10, 26, 10])
  new DataView(bytes.buffer).setUint32(8, 13)
  bytes.set([73, 72, 68, 82], 12)
  new DataView(bytes.buffer).setUint32(16, width)
  new DataView(bytes.buffer).setUint32(20, height)
  return `data:image/png;base64,${btoa(String.fromCharCode(...bytes))}`
}
let pixels: Uint8ClampedArray
let loads: boolean
let dimensions: [number, number]
let images: HTMLImageElement[]
let draw: ReturnType<typeof vi.fn>
let revoke: ReturnType<typeof vi.spyOn>
const shot = () => ({ dataUrl: header(), width: 2, height: 2 })
const signal = () => new AbortController().signal

beforeEach(() => {
  pixels = new Uint8ClampedArray([255, 0, 0, 255, 0, 0, 255, 255, 255, 0, 0, 255, 0, 0, 255, 255])
  loads = true; dimensions = [2, 2]; images = []; draw = vi.fn()
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:capture-test')
  revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.spyOn(HTMLImageElement.prototype, 'src', 'set').mockImplementation(function (this: HTMLImageElement) {
    images.push(this)
    Object.defineProperties(this, { naturalWidth: { get: () => dimensions[0] }, naturalHeight: { get: () => dimensions[1] } })
    if (loads) queueMicrotask(() => this.dispatchEvent(new Event('load')))
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw,
    getImageData: () => ({ data: pixels }) } as unknown as CanvasRenderingContext2D)
})
afterEach(() => vi.restoreAllMocks())

describe('Visualize PNG validation', () => {
  it('returns PNG bytes only after decoder dimensions and visible pixels are checked', async () => {
    const png = await validateCapturePng(shot(), signal())
    expect(png.type).toBe('image/png')
    expect([...new Uint8Array(await png.arrayBuffer()).slice(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10])
    expect(draw).toHaveBeenCalledOnce()
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:capture-test')
    expect(document.querySelector('img, canvas')).toBeNull()
  })
  it.each(['data:text/html;base64,PGgxPk5vPC9oMT4=', 'data:image/png;base64,PGgxPk5vPC9oMT4=', 'data:image/png;base64,%%%=', 'data:image/svg+xml;base64,PHN2Zz4='])('rejects mislabeled or malformed image data before decoding: %s', async dataUrl => {
    await expect(validateCapturePng({ ...shot(), dataUrl }, signal())).rejects.toMatchObject({ kind: 'invalidPng' })
    expect(images).toHaveLength(0)
  })
  it('rejects oversized bytes and encoded pixel dimensions before image allocation', async () => {
    const dataUrl = 'data:image/png;base64,' + 'A'.repeat(Math.ceil(VISUALIZE_CAPTURE_MAX_BYTES / 3) * 4 + 4)
    await expect(validateCapturePng({ ...shot(), dataUrl }, signal())).rejects.toMatchObject({ kind: 'tooLarge' })
    await expect(validateCapturePng({ ...shot(), dataUrl: header(4097, 2) }, signal())).rejects.toMatchObject({ kind: 'tooLarge' })
    expect(images).toHaveLength(0)
  })
  it('rejects wholly transparent pixels but preserves legitimate solid-color canvases', async () => {
    pixels.fill(0)
    await expect(validateCapturePng(shot(), signal())).rejects.toMatchObject({ kind: 'blank' })
    pixels.fill(255)
    await expect(validateCapturePng(shot(), signal())).resolves.toMatchObject({ type: 'image/png' })
  })
  it('does not trust a PNG header when the image decoder disagrees or fails', async () => {
    dimensions = [8, 8]
    await expect(validateCapturePng(shot(), signal())).rejects.toMatchObject({ kind: 'invalidPng' })
    loads = false
    const pending = validateCapturePng(shot(), signal())
    const rejected = expect(pending).rejects.toMatchObject({ kind: 'invalidPng' })
    images[images.length - 1]!.dispatchEvent(new Event('error'))
    await rejected
  })
  it('revokes decoding resources and rejects a cancelled preparation', async () => {
    loads = false
    const controller = new AbortController()
    const pending = validateCapturePng(shot(), controller.signal)
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    controller.abort()
    await rejected
    expect(revoke).toHaveBeenCalledOnce()
    expect(images[0]!.getAttribute('src')).toBeNull()
  })
  it('bounds source layout before the upstream minimum-scale rasterization', () => {
    expect(() => checkCaptureLayout(1200, 600)).not.toThrow()
    expect(() => checkCaptureLayout(1000, 17_000)).toThrow('size limit')
    expect(() => checkCaptureLayout(10_000, 10_000)).toThrow('size limit')
    expect(() => checkCaptureLayout(0, 0)).toThrow('empty')
  })
})
