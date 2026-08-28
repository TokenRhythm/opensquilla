import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createPrivateHttpTransport,
  HttpTransportError,
} from './privateHttpTransport'

interface FetchMockLike {
  mock: { calls: Array<[RequestInfo | URL, RequestInit?]> }
}

function requestInit(fetchMock: FetchMockLike): RequestInit {
  const init = fetchMock.mock.calls[0]?.[1]
  if (!init || typeof init !== 'object') throw new Error('missing fetch init')
  return init as RequestInit
}

afterEach(() => {
  vi.useRealTimers()
})

describe('private Gateway HTTP transport', () => {
  it('owns base resolution, auth headers, session fencing, and JSON encoding', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(
      JSON.stringify({ item: 'session-a' }),
      { headers: { 'content-type': 'application/json' } },
    ))
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/control/',
      authToken: () => '  secret-token  ',
      fetch: fetchMock,
    })

    await expect(transport.requestJson('/api/v1/sessions', {
      method: 'POST',
      sessionKey: '  session-a  ',
      json: { limit: 25 },
    })).resolves.toEqual({ item: 'session-a' })

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      'https://control.example/api/v1/sessions',
    )
    const init = requestInit(fetchMock)
    const headers = new Headers(init.headers)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ limit: 25 }))
    expect(headers.get('content-type')).toBe('application/json')
    expect(headers.get('authorization')).toBe('Bearer secret-token')
    expect(headers.get('x-opensquilla-session-key')).toBe('session-a')
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it('passes form bodies through without inventing a content type', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', {
      headers: { 'content-type': 'application/json' },
    }))
    const form = new FormData()
    form.set('file', new Blob(['payload']), 'payload.txt')
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      authToken: () => '',
      fetch: fetchMock,
    })

    await transport.requestJson('/api/v1/files/upload', {
      method: 'POST',
      form,
    })

    const init = requestInit(fetchMock)
    expect(init.body).toBe(form)
    expect(new Headers(init.headers).has('content-type')).toBe(false)
  })

  it('decodes successful blobs without exposing Response to callers', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response('archive-bytes')
    ))
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    const blob = await transport.requestBlob('/api/v1/diagnostics/bundle')

    expect(await blob.text()).toBe('archive-bytes')
  })

  it('maps HTTP status and safe response payload into one stable error', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(
      JSON.stringify({ code: 'forbidden' }),
      {
        status: 403,
        headers: { 'content-type': 'application/json' },
      },
    ))
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    const failure = await transport.requestJson('/api/private').catch(error => error)

    expect(failure).toBeInstanceOf(HttpTransportError)
    expect(failure).toMatchObject({
      kind: 'http-status',
      status: 403,
      payload: { code: 'forbidden' },
    })
    expect(failure).not.toHaveProperty('response')
  })

  it('classifies invalid JSON without leaking a native parsing exception', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => (
      new Response('not-json', { status: 200 })
    ))
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    await expect(transport.requestJson('/api/value')).rejects.toMatchObject({
      name: 'HttpTransportError',
      kind: 'decode',
      status: 200,
    })
  })

  it('classifies JSON encoding failures before issuing a request', async () => {
    const fetchMock = vi.fn()
    const circular: { self?: unknown } = {}
    circular.self = circular
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    await expect(transport.requestJson('/api/value', {
      method: 'POST',
      json: circular,
    })).rejects.toMatchObject({ kind: 'encode' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('distinguishes caller cancellation from transport timeout', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>(
      (_resolve, reject) => init?.signal?.addEventListener(
        'abort',
        () => reject(new DOMException('aborted', 'AbortError')),
        { once: true },
      ),
    ))
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 10,
    })

    const timedOut = transport.requestJson('/api/slow')
    const timedOutAssertion = expect(timedOut).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(10)
    await timedOutAssertion

    const controller = new AbortController()
    const cancelled = transport.requestJson('/api/cancelled', {
      signal: controller.signal,
      timeoutMs: 0,
    })
    const cancelledAssertion = expect(cancelled).rejects.toMatchObject({ kind: 'aborted' })
    controller.abort('route-left')
    await cancelledAssertion
  })

  it('keeps the timeout active until response decoding finishes', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: () => new Promise<unknown>((_resolve, reject) => (
        init?.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      )),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 10,
    })

    const decoding = transport.requestJson('/api/slow-body')
    const assertion = expect(decoding).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(10)
    await assertion
  })

  it('rejects cross-origin, credentialed, and non-HTTP endpoints before fetch', async () => {
    const fetchMock = vi.fn()
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    for (const endpoint of [
      'https://attacker.example/api',
      'https://user:pass@control.example/api',
      'data:application/json,{}',
      'blob:https://control.example/id',
      '',
    ]) {
      await expect(transport.requestJson(endpoint)).rejects.toMatchObject({
        kind: 'invalid-endpoint',
      })
    }
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('maps an ordinary fetch rejection to a network error', async () => {
    const cause = new TypeError('offline')
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => { throw cause }),
    })

    await expect(transport.requestJson('/api/value')).rejects.toMatchObject({
      kind: 'network',
      transportCause: cause,
    })
  })
})
