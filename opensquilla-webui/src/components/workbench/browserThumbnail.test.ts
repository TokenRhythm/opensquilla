// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createBrowserThumbnail } from './browserThumbnail'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('downsizes captured pixels, bounds retained bytes and closes the decoded bitmap', async () => {
  const bitmap = { width: 1600, height: 1200, close: vi.fn() }
  vi.stubGlobal('createImageBitmap', vi.fn(async () => bitmap))
  const draw = vi.fn()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['jpeg'])))
  const objectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:small-preview')
  expect(await createBrowserThumbnail({ targetRef: 'page-1', mimeType: 'image/png',
    dataBase64: 'QQ==', width: 1600, height: 1200 })).toBe('blob:small-preview')
  expect(draw).toHaveBeenCalledWith(bitmap, 0, 0, 1067, 800)
  expect(bitmap.close).toHaveBeenCalledOnce()
  expect(objectUrl).toHaveBeenCalledOnce()
  vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation(callback => callback(new Blob([new Uint8Array(1048577)])))
  expect(await createBrowserThumbnail({ targetRef: 'page-1', mimeType: 'image/png',
    dataBase64: 'QQ==', width: 1600, height: 1200 })).toBeNull()
  expect(objectUrl).toHaveBeenCalledOnce()
  expect(bitmap.close).toHaveBeenCalledTimes(2)
})

it('degrades to metadata when bitmap decoding is unavailable', async () => {
  vi.stubGlobal('createImageBitmap', undefined)
  expect(await createBrowserThumbnail({ targetRef: 'page-1', mimeType: 'image/png',
    dataBase64: 'QQ==', width: 100, height: 100 })).toBeNull()
})
