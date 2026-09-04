// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createArtifactPreview } from '@/adapters/gateway/artifactPreviewV4'
import {
  httpTransportTestDouble,
  type TestHttpBinaryResponse,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'

function httpTransport(requestBinary: TestHttpTransport['requestBinary']): TestHttpTransport {
  return httpTransportTestDouble({ requestBinary })
}

afterEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('createArtifactPreview', () => {
  it('derives the sanitized endpoint and scope for the private transport', async () => {
    const blob = new Blob(['image-bytes'], { type: 'image/png' })
    const response: TestHttpBinaryResponse = {
      metadata: { status: 200, contentLength: blob.size, contentType: blob.type },
      blob: async () => blob,
      stream: () => blob.stream(),
    }
    const requestBinary = vi.fn(async () => response)
    const http = httpTransport(requestBinary)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:artifact-image')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const controller = createArtifactPreview(http, {
      artifact: () => ({
        id: 'image',
        name: 'image.png',
        mime: 'image/png',
        download_url: '/api/v1/artifacts/image?token=stale&sessionKey=stale',
      }),
      sessionKey: () => 'agent:main:webchat:ok',
    })

    controller.load()
    await vi.waitFor(() => expect(controller.state.value).toBe('loaded'))

    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/image', {
      sessionKey: 'agent:main:webchat:ok',
      signal: expect.any(AbortSignal),
      timeoutMs: 0,
    })
    controller.dispose()
  })

  it('aborts an active preview request when disposed', async () => {
    const observed: { signal?: AbortSignal } = {}
    const requestBinary = vi.fn(
      (_input: string, options?: Parameters<TestHttpTransport['requestBinary']>[1]) => {
        observed.signal = options?.signal
        return new Promise<TestHttpBinaryResponse>((_resolve, reject) => {
          observed.signal?.addEventListener('abort', () => {
            reject(new DOMException('cancelled', 'AbortError'))
          }, { once: true })
        })
      },
    )
    const controller = createArtifactPreview(httpTransport(requestBinary), {
      artifact: () => ({
        id: 'image',
        name: 'image.png',
        mime: 'image/png',
      }),
      timeoutMs: 60_000,
    })

    controller.load()
    await vi.waitFor(() => expect(requestBinary).toHaveBeenCalledOnce())
    expect(observed.signal?.aborted).toBe(false)

    controller.dispose()

    expect(observed.signal?.aborted).toBe(true)
    expect(observed.signal?.reason).toBe('cancelled')
    expect(controller.state.value).toBe('idle')
    await Promise.resolve()
  })

  it('loads cross-origin previews through the credential-free artifact capability', async () => {
    const blob = new Blob(['external-image'], { type: 'image/png' })
    const response: TestHttpBinaryResponse = {
      metadata: { status: 200, contentLength: blob.size, contentType: blob.type },
      blob: async () => blob,
      stream: () => blob.stream(),
    }
    const requestBinary = vi.fn(async () => response)
    const fetchExternalArtifact = vi.fn(async () => response)
    const endpoint = 'https://files.example.test/image.png?signature=fixture'
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:external-image')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const controller = createArtifactPreview(
      httpTransportTestDouble({ fetchExternalArtifact, requestBinary }),
      {
        artifact: () => ({
          id: 'external-image',
          name: 'image.png',
          mime: 'image/png',
          download_url: endpoint,
        }),
      },
      () => 'http://127.0.0.1:18791',
    )

    controller.load()
    await vi.waitFor(() => expect(controller.state.value).toBe('loaded'))

    expect(fetchExternalArtifact).toHaveBeenCalledWith(endpoint, expect.any(AbortSignal))
    expect(requestBinary).not.toHaveBeenCalled()
    controller.dispose()
  })
})
