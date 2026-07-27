// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createArtifactPreview } from './useArtifactPreview'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('createArtifactPreview', () => {
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
      resolveUrl: () => '/api/v1/artifacts/image',
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
