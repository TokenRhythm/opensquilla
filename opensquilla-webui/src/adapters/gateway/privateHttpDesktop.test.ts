import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindArtifactBinaryRequest } from './privateArtifactHttpTransport'
import { createPrivateHttpTransport } from './privateHttpTransport'

afterEach(() => vi.unstubAllGlobals())

describe('Desktop Gateway HTTP composition', () => {
  it('downloads immutable artifact bytes through the exact Desktop API proxy', async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response('immutable report', { headers: { 'content-type': 'text/plain' } })
    ))
    const http = createPrivateHttpTransport({
      baseUrl: 'opensquilla-app://desktop/chat?session=session-a',
      authToken: () => '',
      fetch,
    })
    const request = bindArtifactBinaryRequest(http, {
      id: 'artifact-1',
      name: 'report.txt',
      mime: 'text/plain',
      download_url: '/api/v1/artifacts/artifact-1?token=old&sessionKey=old',
    }, { baseOrigin: 'opensquilla-app://desktop', policy: 'allow-external' })

    expect(request).not.toBeNull()
    const response = await request!.execute({ sessionKey: 'session-a' })
    expect(response.metadata.status).toBe(200)
    expect(await (await response.blob()).text()).toBe('immutable report')
    expect(String(fetch.mock.calls[0]?.[0])).toBe(
      'opensquilla-app://desktop/api/v1/artifacts/artifact-1',
    )
    const headers = new Headers(fetch.mock.calls[0]?.[1]?.headers)
    expect(headers.get('x-opensquilla-session-key')).toBe('session-a')
    expect(headers.has('authorization')).toBe(false)
  })

  it.each(['/api', '/api/v1/status', 'opensquilla-app://desktop/api/v1/status']) (
    'accepts the exact Desktop API capability: %s', async (endpoint) => {
      const fetch = vi.fn(async () => new Response('{}'))
      const http = createPrivateHttpTransport({ baseUrl: 'opensquilla-app://desktop/chat', fetch })
      await expect(http.requestJson(endpoint)).resolves.toEqual({})
      expect(fetch).toHaveBeenCalledTimes(1)
    },
  )

  it.each([
    'opensquilla-app://attacker/api/v1/status',
    'opensquilla-app://desktop:80/api/v1/status',
    'opensquilla-app://user@desktop/api/v1/status',
    'opensquilla-app://:pass@desktop/api/v1/status',
    'opensquilla-app://desktop.attacker/api/v1/status',
    'other-app://desktop/api/v1/status',
    'file:///api/v1/status',
    'data:application/json,{}',
    'https://desktop/api/v1/status',
    'http://127.0.0.1:18791/api/v1/status',
    '//attacker/api/v1/status',
    '/chat',
    '/static/img/icon.svg',
    '/api-not-allowed',
    '/api/../chat',
    '/api/%2e%2e/chat',
  ])('rejects an endpoint outside the Desktop API capability before auth/fetch: %s', async (endpoint) => {
    const fetch = vi.fn()
    const authToken = vi.fn(() => 'must-not-leak')
    const http = createPrivateHttpTransport({
      baseUrl: 'opensquilla-app://desktop/chat', fetch, authToken,
    })
    await expect(http.requestJson(endpoint)).rejects.toMatchObject({ kind: 'invalid-endpoint' })
    expect(fetch).not.toHaveBeenCalled()
    expect(authToken).not.toHaveBeenCalled()
  })

  it.each([
    'opensquilla-app://attacker/chat',
    'opensquilla-app://desktop:80/chat',
    'opensquilla-app://user@desktop/chat',
    'opensquilla-app://:pass@desktop/chat',
    'other-app://desktop/chat',
    'file:///chat',
  ])('does not grant Desktop access from an untrusted base: %s', async (baseUrl) => {
    const fetch = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl, fetch })
    await expect(http.requestJson('opensquilla-app://desktop/api/v1/status'))
      .rejects.toMatchObject({ kind: 'invalid-endpoint' })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps HTTP origins isolated from the Desktop proxy', async () => {
    const fetch = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl: 'https://control.example/', fetch })
    await expect(http.requestJson('opensquilla-app://desktop/api/v1/status'))
      .rejects.toMatchObject({ kind: 'invalid-endpoint' })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('accepts a native form without consulting the fallback brand', async () => {
    const form = new FormData()
    form.set('name', 'report')
    vi.stubGlobal('URLSearchParams', new Proxy(URLSearchParams, {
      get() { throw new Error('fallback brand must not be consulted') },
    }))
    const fetch = vi.fn(async () => new Response('{}'))
    const http = createPrivateHttpTransport({ baseUrl: 'https://control.example/', fetch })
    await expect(http.requestJson('/api/upload', { method: 'POST', form })).resolves.toEqual({})
  })

  it('uses native URLSearchParams when FormData is unavailable', async () => {
    const form = new URLSearchParams('name=report')
    vi.stubGlobal('FormData', undefined)
    const fetch = vi.fn(async () => new Response('{}'))
    const http = createPrivateHttpTransport({ baseUrl: 'https://control.example/', fetch })
    await expect(http.requestJson('/api/upload', { method: 'POST', form })).resolves.toEqual({})
  })

  it('rejects revoked form proxies before fetch', async () => {
    const { proxy, revoke } = Proxy.revocable(new FormData(), {})
    revoke()
    const fetch = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl: 'https://control.example/', fetch })
    await expect(http.requestJson('/api/upload', { method: 'POST', form: proxy }))
      .rejects.toMatchObject({ kind: 'encode' })
    expect(fetch).not.toHaveBeenCalled()
  })
})
