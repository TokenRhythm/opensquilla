// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createArtifactPreview } from './useArtifactPreview'

afterEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('createArtifactPreview', () => {
  it('derives the sanitized endpoint and credentials from an artifact session request', async () => {
    sessionStorage.setItem('opensquilla.wsToken', 'adapter-secret')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('image-bytes', {
        status: 200,
        headers: { 'content-type': 'image/png' },
      }),
    )
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:artifact-image')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const controller = createArtifactPreview({
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

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/artifacts/image', {
      method: 'GET',
      headers: {
        'x-opensquilla-session-key': 'agent:main:webchat:ok',
        Authorization: 'Bearer adapter-secret',
      },
      credentials: 'same-origin',
      signal: expect.any(AbortSignal),
      redirect: 'error',
    })
    controller.dispose()
  })

  it('aborts an active preview request when disposed', async () => {
    const observed: { signal?: AbortSignal } = {}
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) => {
        observed.signal = init?.signal as AbortSignal
        return new Promise<Response>((_resolve, reject) => {
          observed.signal?.addEventListener('abort', () => {
            reject(new DOMException('cancelled', 'AbortError'))
          }, { once: true })
        })
      },
    )
    const controller = createArtifactPreview({
      artifact: () => ({
        id: 'image',
        name: 'image.png',
        mime: 'image/png',
      }),
      timeoutMs: 60_000,
    })

    controller.load()
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
    expect(observed.signal?.aborted).toBe(false)

    controller.dispose()

    expect(observed.signal?.aborted).toBe(true)
    expect(observed.signal?.reason).toBe('cancelled')
    expect(controller.state.value).toBe('idle')
    await Promise.resolve()
  })
})
