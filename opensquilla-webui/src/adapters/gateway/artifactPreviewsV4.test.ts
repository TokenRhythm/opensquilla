import { afterEach, describe, expect, it, vi } from 'vitest'
import { createV4ArtifactPreviews } from './artifactPreviewsV4'
import { createPrivateHttpTransport, HttpTransportError } from './privateHttpTransport'
import { httpTransportTestDouble, type TestHttpTransport } from '@/testing/httpTransport.test-helper'

const origin = 'https://gateway.example'

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function clock() {
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(0.5)
}

describe('Web preview lease cleanup', () => {
  it.each([
    new HttpTransportError('http-status', 'Busy', 429),
    new HttpTransportError('http-status', 'Unavailable', 503),
    new HttpTransportError('network', 'Offline'),
    new HttpTransportError('timeout', 'Timed out'),
  ])('retries $kind/$status outside the disposed panel', async error => {
    clock()
    const requestBlob = vi.fn()
      .mockRejectedValueOnce(error)
      .mockResolvedValue(new Blob())
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })

    await expect(previews.revokeLease('apl-first', { sessionKey: 'session-a' })).rejects.toThrow()
    expect(requestBlob).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(requestBlob).toHaveBeenCalledTimes(2)
    expect(requestBlob).toHaveBeenLastCalledWith(`${origin}/api/v1/artifact-preview-leases/apl-first`, {
      keepalive: true, method: 'DELETE', sessionKey: 'session-a', timeoutMs: 15_000,
    })
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([404, 410])('finishes cleanup when retry confirms status %s', async status => {
    clock()
    const requestBlob = vi.fn()
      .mockRejectedValueOnce(new HttpTransportError('http-status', 'Busy', 429))
      .mockRejectedValue(new HttpTransportError('http-status', 'Gone', status))
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })
    await expect(previews.revokeLease('apl-gone')).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(requestBlob).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('times out a hung DELETE and retries through the real HTTP transport', async () => {
    clock()
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockImplementationOnce((_url, request) => new Promise((_resolve, reject) => {
        request!.signal!.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'))
        }, { once: true })
      }))
      .mockResolvedValue(new Response(null, { status: 204 }))
    const http = createPrivateHttpTransport({ baseUrl: origin, fetch })
    const previews = createV4ArtifactPreviews(http, { baseOrigin: () => origin })
    const initialFailure = expect(previews.revokeLease('apl-timeout')).rejects.toMatchObject({
      retryable: true,
    })
    await vi.advanceTimersByTimeAsync(15_000)
    await initialFailure
    expect(fetch).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('deduplicates in-flight requests without mixing session cleanup', async () => {
    clock()
    let finish!: (value: Blob) => void
    const requestBlob = vi.fn<TestHttpTransport['requestBlob']>(
      () => new Promise<Blob>(resolve => { finish = resolve }),
    )
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })
    const first = previews.revokeLease('apl-first', { sessionKey: 'session-a' })
    const repeated = previews.revokeLease('apl-first', { sessionKey: 'session-a' })
    await Promise.resolve()
    expect(requestBlob).toHaveBeenCalledOnce()
    finish(new Blob())
    await Promise.all([first, repeated])

    requestBlob.mockResolvedValue(new Blob())
    await previews.revokeLease('apl-first', { sessionKey: 'session-b' })
    expect(requestBlob).toHaveBeenCalledTimes(2)
    expect(requestBlob.mock.calls[1]?.[1]).toMatchObject({ sessionKey: 'session-b' })
  })

  it('keeps pending cleanup beyond the last reported eight-hour idle lifetime', async () => {
    clock()
    const requestBlob = vi.fn().mockRejectedValue(new HttpTransportError('network', 'Offline'))
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })
    await expect(previews.revokeLease('apl-pending')).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(9 * 60 * 60 * 1000)
    expect(requestBlob.mock.calls.length).toBeGreaterThan(10)
    expect(vi.getTimerCount()).toBe(1)
    requestBlob.mockResolvedValue(new Blob())
    await vi.advanceTimersByTimeAsync(300_000)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('pauses old cleanup while a different Gateway is selected', async () => {
    clock()
    let currentOrigin = origin
    const requestBlob = vi.fn()
      .mockRejectedValueOnce(new HttpTransportError('network', 'Offline'))
      .mockResolvedValue(new Blob())
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => currentOrigin,
    })
    await expect(previews.revokeLease('apl-old', { sessionKey: 'old-session' })).rejects.toThrow()
    currentOrigin = 'https://another-gateway.example'
    await vi.advanceTimersByTimeAsync(600_000)
    expect(requestBlob).toHaveBeenCalledOnce()
    currentOrigin = origin
    await vi.advanceTimersByTimeAsync(300_000)
    expect(requestBlob).toHaveBeenCalledTimes(2)
    expect(requestBlob.mock.calls[1]?.[0]).toBe(`${origin}/api/v1/artifact-preview-leases/apl-old`)
  })

  it.each([401, 403])('retains auth-blocked cleanup at status %s and reads fresh auth', async status => {
    clock()
    let authToken = 'synthetic-old-token'
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status }))
      .mockResolvedValue(new Response(null, { status: 204 }))
    const http = createPrivateHttpTransport({ baseUrl: origin, authToken: () => authToken, fetch })
    const previews = createV4ArtifactPreviews(http, { baseOrigin: () => origin })
    await expect(previews.revokeLease('apl-auth')).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(299_000)
    expect(fetch).toHaveBeenCalledOnce()
    authToken = 'synthetic-new-token'
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[1]?.[1].headers.get('Authorization')).toBe('Bearer synthetic-new-token')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not retry invalid requests automatically', async () => {
    clock()
    const requestBlob = vi.fn().mockRejectedValue(new HttpTransportError('http-status', 'Invalid', 400))
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })
    await expect(previews.revokeLease('apl-invalid')).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(600_000)
    expect(requestBlob).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
    requestBlob.mockResolvedValue(new Blob())
    await previews.revokeLease('apl-invalid')
    expect(requestBlob).toHaveBeenCalledTimes(2)
  })

  it('leaves Desktop retries to the broker', async () => {
    clock()
    const requestBlob = vi.fn()
    const revoke = vi.fn(async () => ({ ok: false as const, status: 503,
      code: 'PREVIEW_BROKER_UNAVAILABLE', message: 'Unavailable' }))
    const previews = createV4ArtifactPreviews(httpTransportTestDouble({ requestBlob }), {
      baseOrigin: () => origin,
    })
    await expect(previews.revokeLease('apl-desktop', {
      nativeBroker: { revokeArtifactPreviewLease: revoke }, sessionKey: 'desktop-session',
    })).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(600_000)
    expect(revoke).toHaveBeenCalledOnce()
    expect(requestBlob).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
})
