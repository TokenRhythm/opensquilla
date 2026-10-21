// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readBundledStaticAsset } from './staticAssets'

const fetcher = vi.fn()
beforeEach(() => {
  vi.stubGlobal('location', { href: 'https://preview.example/control/chat' })
  vi.stubGlobal('fetch', fetcher)
  fetcher.mockResolvedValue({ ok: true, arrayBuffer: async () => new Uint8Array([7, 8]).buffer })
})
afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })
describe('bundled static asset boundary', () => {
  it.each(['/control/assets/font.pfb', '/assets/font.ttf', '/node_modules/pdfjs-dist/cmaps/Adobe.bcmap', '/@fs/C:/app/font.ttf', 'data:application/octet-stream;base64,AA=='])('reads %s without credentials or redirects', async source => {
    const signal = new AbortController().signal
    expect(await readBundledStaticAsset(source, signal)).toEqual(new Uint8Array([7, 8]))
    expect(fetcher).toHaveBeenCalledWith(new URL(source, location.href).href, { signal, credentials: 'omit', redirect: 'error' })
  })
  it.each(['https://other.example/assets/a', '/api/assets/a', '/control/api/assets/a', '/control/chat', 'https://user:pass@preview.example/assets/a', 'file:///assets/a'])('rejects unbundled resource %s', async source => {
    await expect(readBundledStaticAsset(source, new AbortController().signal)).rejects.toThrow('Not a bundled static resource')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('supports packaged application URLs on the same scheme and host', async () => {
    vi.stubGlobal('location', { href: 'opensquilla-app://desktop/index.html' })
    await readBundledStaticAsset('opensquilla-app://desktop/assets/a.pfb', new AbortController().signal)
    expect(fetcher).toHaveBeenCalledOnce()
    await expect(readBundledStaticAsset('opensquilla-app://other/assets/a.pfb', new AbortController().signal)).rejects.toThrow()
  })
  it('aborts before fetching and rejects failed responses', async () => {
    const controller = new AbortController(); controller.abort()
    await expect(readBundledStaticAsset('/assets/a', controller.signal)).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
    fetcher.mockResolvedValueOnce({ ok: false })
    await expect(readBundledStaticAsset('/assets/a', new AbortController().signal)).rejects.toThrow('Static resource unavailable')
  })
})
