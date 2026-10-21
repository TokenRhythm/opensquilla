// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPdfLoadingTask, pdfCanvasSize, PDF_MAX_CANVAS_PIXELS, PDF_MAX_CANVAS_SIDE } from './pdfPreview'
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist'

vi.mock('pdfjs-dist', () => ({ getDocument: vi.fn(() => ({})), GlobalWorkerOptions: { workerSrc: '' } }))
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('PDF loading policy', () => {
  it('uses the bundled worker, isolated bytes, local resource factories, and no active PDF content', async () => {
    const source = new Uint8Array([1, 2, 3]).buffer, controller = new AbortController()
    await createPdfLoadingTask(source, controller.signal)
    const options = vi.mocked(getDocument).mock.calls[0]![0] as any
    expect(options).toMatchObject({ isEvalSupported: false, enableXfa: false, useSystemFonts: false,
      useWorkerFetch: false, disableAutoFetch: true, disableStream: true, disableRange: true })
    expect(options.data).toEqual(new Uint8Array(source))
    expect(options.data.buffer).not.toBe(source)
    expect(GlobalWorkerOptions.workerSrc).toContain('pdf.worker.min.mjs')
    const fetcher = vi.fn().mockResolvedValue({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) })
    vi.stubGlobal('fetch', fetcher)
    vi.stubGlobal('location', { href: 'https://preview.example/control/chat' })
    await new options.CMapReaderFactory().fetch({ name: 'Adobe-Japan1-UCS2' })
    await new options.StandardFontDataFactory().fetch({ filename: 'LiberationSans-Regular.ttf' })
    await new options.WasmFactory().fetch({ filename: 'openjpeg.wasm' })
    expect(fetcher).toHaveBeenCalledTimes(3)
    for (const [url, init] of fetcher.mock.calls) {
      expect(url).toMatch(/^(?:https:\/\/preview\.example\/|data:)/)
      expect(init.signal).toBe(controller.signal)
      expect(init.credentials).toBe('omit')
    }
    await expect(new options.CMapReaderFactory().fetch({ name: '../../https://evil.example/font' })).rejects.toThrow('Unknown PDF resource')
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('does not create a worker when a delayed import finishes after cancellation', async () => {
    const controller = new AbortController(); controller.abort()
    await expect(createPdfLoadingTask(new ArrayBuffer(1), controller.signal)).rejects.toThrow()
    expect(getDocument).not.toHaveBeenCalled()
  })

  it.each([[600, 800, 2], [1_000_000, 100, 4], [100, 1_000_000, 4], [1e12, 1e12, 4]])('caps physical canvas for %d by %d', (width, height, dpr) => {
    const result = pdfCanvasSize(width, height, dpr)
    expect(result.width * result.height).toBeLessThanOrEqual(PDF_MAX_CANVAS_PIXELS)
    expect(Math.max(result.width, result.height)).toBeLessThanOrEqual(PDF_MAX_CANVAS_SIDE)
    expect(result.width).toBeGreaterThan(0); expect(result.height).toBeGreaterThan(0)
  })
  it.each([[0, 5], [5, -1], [Infinity, 5], [5, NaN]])('rejects invalid PDF page dimensions %d by %d', (width, height) => {
    expect(() => pdfCanvasSize(width, height, 1)).toThrow('Invalid PDF page dimensions')
  })
})
