// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactPayload } from '@/types/rpc'
import {
  ARTIFACT_TEXT_PREVIEW_LIMIT,
} from '@/utils/workbench/artifactPreview'
import {
  createArtifactPreviewResource,
  type NativeHtmlArtifactResource,
} from './useArtifactPreviewResource'

function artifact(overrides: Partial<ArtifactPayload> = {}): ArtifactPayload {
  return {
    id: 'artifact-1',
    name: 'notes.txt',
    mime: 'text/plain',
    download_url: '/api/v1/artifacts/artifact-1',
    ...overrides,
  }
}

function response(body: BodyInit, mime: string): Response {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': mime },
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(next => { resolve = next })
  return { promise, resolve }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('createArtifactPreviewResource', () => {
  it('aborts the active request synchronously when disposed', () => {
    const observed: { signal?: AbortSignal } = {}
    const fetchImpl = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      observed.signal = init?.signal as AbortSignal
      return new Promise<Response>(() => undefined)
    })
    const controller = createArtifactPreviewResource({
      artifact: () => artifact(),
      fetchImpl: fetchImpl as typeof fetch,
    })

    void controller.load()
    expect(observed.signal?.aborted).toBe(false)

    controller.dispose()
    expect(observed.signal?.aborted).toBe(true)
    expect(controller.state.value).toBe('idle')
  })

  it('ignores a stale response that resolves after a newer artifact load', async () => {
    const first = deferred<Response>()
    const second = deferred<Response>()
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const controller = createArtifactPreviewResource({
      artifact: () => artifact(),
      fetchImpl,
    })

    const firstLoad = controller.load()
    const secondLoad = controller.reload()
    second.resolve(response('new result', 'text/plain'))
    await secondLoad
    first.resolve(response('stale result', 'text/plain'))
    await firstLoad

    expect(controller.text.value).toBe('new result')
    expect(controller.state.value).toBe('ready')
  })

  it('revokes object URLs on reload and disposal', async () => {
    const createObjectUrl = vi.fn()
      .mockReturnValueOnce('blob:first')
      .mockReturnValueOnce('blob:second')
    const revokeObjectUrl = vi.fn()
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response(new Uint8Array([1]), 'image/png'))
      .mockResolvedValueOnce(response(new Uint8Array([2]), 'image/png'))
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'image.png', mime: 'image/png' }),
      createObjectUrl,
      fetchImpl,
      revokeObjectUrl,
    })

    await controller.load()
    expect(controller.objectUrl.value).toBe('blob:first')

    await controller.reload()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:first')
    expect(controller.objectUrl.value).toBe('blob:second')

    controller.dispose()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:second')
  })

  it('rejects oversized text before downloading it', async () => {
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ size: ARTIFACT_TEXT_PREVIEW_LIMIT + 1 }),
      fetchImpl,
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('unsupported')
    expect(controller.errorCode.value).toBe('too-large')
  })

  it('does not fetch preview bytes from an untrusted origin', async () => {
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ download_url: 'https://files.invalid/artifact-1' }),
      baseOrigin: () => 'https://control.example',
      fetchImpl,
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('missing-url')
  })

  it('decodes a server-validated inline UTF-8 HTML attachment without fetch', async () => {
    const observed: { blob?: Blob } = {}
    const inlineUrl = 'data:text/html;charset=utf-8;base64,PCFkb2N0eXBlIGh0bWw+PGgxPuS9oOWlve+8jElubGluZSDimJU8L2gxPg=='
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        id: undefined,
        name: 'inline.html',
        mime: 'text/html',
        sha256: 'f65982a5bbec7babf0dfda6e60b973dd750a40545a8f52f62a924a5d7c8184e2',
        size: 43,
        download_url: inlineUrl,
        workbenchResourceType: 'attachment',
      }),
      createObjectUrl: blob => {
        observed.blob = blob
        return 'blob:inline-preview'
      },
      fetchImpl,
      revokeObjectUrl: vi.fn(),
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('ready')
    expect(controller.objectUrl.value).toBe('blob:inline-preview')
    expect(await observed.blob?.text()).toContain('你好，Inline ☕')
    expect(await observed.blob?.text()).toContain("connect-src 'none'")
  })

  it('fails closed when an inline attachment contains malformed base64', async () => {
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        id: undefined,
        name: 'inline.html',
        mime: 'text/html',
        download_url: 'data:text/html;base64,abcde',
        workbenchResourceType: 'attachment',
      }),
      fetchImpl,
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('invalid-content')
  })

  it('rejects an oversized inline attachment before decoding or fetching it', async () => {
    const fetchImpl = vi.fn()
    const encoded = 'AAAA'.repeat(Math.ceil(ARTIFACT_TEXT_PREVIEW_LIMIT / 3) + 1)
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        id: undefined,
        name: 'inline.html',
        mime: 'text/html',
        download_url: `data:text/html;base64,${encoded}`,
        workbenchResourceType: 'attachment',
      }),
      fetchImpl,
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('unsupported')
    expect(controller.errorCode.value).toBe('too-large')
  })

  it('reports inline attachment integrity mismatches explicitly', async () => {
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        id: undefined,
        name: 'inline.html',
        mime: 'text/html',
        sha256: '0'.repeat(64),
        size: 15,
        download_url: 'data:text/html;base64,PGgxPklubGluZTwvaDE+',
        workbenchResourceType: 'attachment',
      }),
      fetchImpl,
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('integrity-error')
  })

  it('emits native HTML bytes and reports unresolved relative resources', async () => {
    const nativeReady = vi.fn<(resource: NativeHtmlArtifactResource) => void>()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'page.html', mime: 'text/html' }),
      fetchImpl: vi.fn().mockResolvedValue(response(
        '<html><head><link href="./style.css"></head><body>Preview</body></html>',
        'text/html',
      )),
      nativeHtml: () => true,
      onNativeHtmlReady: nativeReady,
      sessionKey: () => 'session:test',
    })

    await controller.load()

    expect(controller.state.value).toBe('missing-resource')
    expect(controller.objectUrl.value).toBe('')
    expect(nativeReady).toHaveBeenCalledOnce()
    const payload = nativeReady.mock.calls[0]?.[0]
    expect(payload?.sessionKey).toBe('session:test')
    expect(payload?.hasRelativeResources).toBe(true)
    expect(new TextDecoder().decode(payload?.data)).toContain('Preview')
  })

  it('uses the explicit ready-with-warnings state for partial bundle leases', async () => {
    const fetchImpl = vi.fn()
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'page.html', mime: 'text/html' }),
      fetchImpl,
      htmlCollectionStatus: () => 'partial',
      htmlLaunchUrl: () => 'https://preview.example.test/index.html',
    })

    await controller.load()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(controller.objectUrl.value).toBe('https://preview.example.test/index.html')
    expect(controller.state.value).toBe('ready-with-warnings')
  })

  it('never downloads the entry HTML while a preview lease is pending or blocked', async () => {
    const fetchImpl = vi.fn()
    let leaseState: 'pending' | 'blocked' = 'pending'
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'page.html', mime: 'text/html' }),
      fetchImpl,
      htmlLeaseState: () => leaseState,
    })

    await controller.load()
    expect(controller.state.value).toBe('loading')
    expect(fetchImpl).not.toHaveBeenCalled()

    leaseState = 'blocked'
    await controller.reload()
    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('preview-blocked')
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('keeps recoverable native load errors distinct from renderer crashes', () => {
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'page.html', mime: 'text/html' }),
      fetchImpl: vi.fn(),
      nativeHtml: () => true,
    })

    controller.markNativeError()
    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('native-error')

    controller.markNativeCrashed()
    expect(controller.state.value).toBe('crashed')
    expect(controller.errorCode.value).toBe('native-crashed')
  })

  it('builds an offline HTML blob for the web renderer', async () => {
    const observed: { blob?: Blob } = {}
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'page.html', mime: 'text/html' }),
      createObjectUrl: blob => {
        observed.blob = blob
        return 'blob:offline-preview'
      },
      fetchImpl: vi.fn().mockResolvedValue(response(
        '<script>document.body.textContent = "ran"</script>',
        'text/html',
      )),
      revokeObjectUrl: vi.fn(),
    })

    await controller.load()

    expect(controller.state.value).toBe('ready')
    expect(controller.objectUrl.value).toBe('blob:offline-preview')
    expect(await observed.blob?.text()).toContain('Content-Security-Policy')
    expect(await observed.blob?.text()).toContain("connect-src 'none'")
  })

  it('marks a mismatched response as invalid instead of rendering it', async () => {
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'image.png', mime: 'image/png' }),
      fetchImpl: vi.fn().mockResolvedValue(response('<script>bad()</script>', 'text/html')),
    })

    await controller.load()

    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('invalid-content')
    expect(controller.objectUrl.value).toBe('')
  })

  it('does not let delayed MIME cancellation overwrite suspension', async () => {
    const cancelStarted = deferred<void>()
    const allowCancel = deferred<void>()
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelStarted.resolve(undefined)
        return allowCancel.promise
      },
    })
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({ name: 'image.png', mime: 'image/png' }),
      fetchImpl: vi.fn().mockResolvedValue(new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      })),
    })

    const load = controller.load()
    await cancelStarted.promise
    controller.suspend()
    allowCancel.resolve(undefined)
    await load

    expect(controller.state.value).toBe('suspended')
    expect(controller.errorCode.value).toBeNull()
  })

  it('reports Gateway artifact integrity failures explicitly', async () => {
    const controller = createArtifactPreviewResource({
      artifact: () => artifact(),
      fetchImpl: vi.fn().mockResolvedValue(new Response(JSON.stringify({
        code: 'INTEGRITY_ERROR',
        error: 'checksum mismatch',
      }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      })),
    })

    await controller.load()

    expect(controller.state.value).toBe('error')
    expect(controller.errorCode.value).toBe('integrity-error')
  })

  it('uses an application/pdf Blob for generic PDF responses', async () => {
    const observed: { blob?: Blob } = {}
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        name: 'report.pdf',
        mime: 'application/octet-stream',
      }),
      createObjectUrl: blob => {
        observed.blob = blob
        return 'blob:pdf-preview'
      },
      fetchImpl: vi.fn().mockResolvedValue(response(
        new Uint8Array([0x25, 0x50, 0x44, 0x46]),
        'application/octet-stream',
      )),
      revokeObjectUrl: vi.fn(),
    })

    await controller.load()

    expect(controller.state.value).toBe('ready')
    expect(controller.objectUrl.value).toBe('blob:pdf-preview')
    expect(observed.blob?.type).toBe('application/pdf')
  })

  it('uses the file extension to type a generic image response', async () => {
    const observed: { blob?: Blob } = {}
    const controller = createArtifactPreviewResource({
      artifact: () => artifact({
        name: 'preview.png',
        mime: 'application/octet-stream',
      }),
      createObjectUrl: blob => {
        observed.blob = blob
        return 'blob:image-preview'
      },
      fetchImpl: vi.fn().mockResolvedValue(response(
        new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
        'application/octet-stream',
      )),
      revokeObjectUrl: vi.fn(),
    })

    await controller.load()

    expect(controller.state.value).toBe('ready')
    expect(observed.blob?.type).toBe('image/png')
  })
})
