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
    expect(init.credentials).toBe('same-origin')
    expect(init.redirect).toBe('error')
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

    const params = new URLSearchParams('name=payload')
    await transport.requestJson('/api/v1/files/query', {
      method: 'POST',
      form: params,
    })

    const init = requestInit(fetchMock)
    expect(init.body).toBe(form)
    expect(new Headers(init.headers).has('content-type')).toBe(false)
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(params)
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

  it('exposes sanitized binary metadata and a one-shot body', async () => {
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => new Response('streamed-bytes', {
        headers: {
          'content-disposition': 'attachment; filename="../bundle.zip"',
          'content-length': '14',
          'content-type': 'application/zip',
          'x-internal-gateway-detail': 'must-not-leak',
        },
      })),
    })

    const binary = await transport.requestBinary('/api/v1/diagnostics/bundle')

    expect(binary.metadata).toEqual({
      status: 200,
      filename: '_bundle.zip',
      contentLength: 14,
      contentType: 'application/zip',
    })
    expect(binary).not.toHaveProperty('headers')
    expect(binary).not.toHaveProperty('response')
    const stream = binary.stream()
    expect(stream).toBeInstanceOf(ReadableStream)
    expect(await new Response(stream).text()).toBe('streamed-bytes')
    await expect(binary.blob()).rejects.toMatchObject({ kind: 'decode' })
  })

  it('parses RFC5987 filenames and neutralizes Windows path hazards', async () => {
    const cases = [
      ["attachment; filename *= UTF-8''b%C3%BCndel.zip", 'bündel.zip'],
      ["attachment; filename=\"ok.zip\"; filename*=UTF-8''%E0%A4%ZZ", 'ok.zip'],
      ['attachment; filename="C:\\logs\\report.zip"', 'logs_report.zip'],
      ['attachment; filename="report.zip:payload"', 'report.zip_payload'],
      ['attachment; filename="CON.txt"', '_CON.txt'],
      ['attachment; filename="CON .txt"', '_CON .txt'],
      ['attachment; filename="COM¹.txt"', '_COM¹.txt'],
    ] as const

    for (const [contentDisposition, filename] of cases) {
      const transport = createPrivateHttpTransport({
        baseUrl: 'https://control.example/',
        fetch: vi.fn(async () => new Response('x', {
          headers: { 'content-disposition': contentDisposition },
        })),
      })
      const binary = await transport.requestBinary('/api/filename')
      expect(binary.metadata.filename).toBe(filename)
      await binary.blob()
    }
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

  it('does not hang on a 4xx body after transport timeout', async () => {
    vi.useFakeTimers()
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 503,
      headers: new Headers({ 'content-type': 'application/json' }),
      body,
      json: () => new Promise<unknown>(() => undefined),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 10,
    })

    const pending = transport.requestJson('/api/hanging-error')
    const assertion = expect(pending).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(10)

    await assertion
    expect(cancelCalled).toBe(true)
  })

  it('does not hang on a 4xx body after caller cancellation', async () => {
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 503,
      headers: new Headers({ 'content-type': 'application/json' }),
      body,
      json: () => new Promise<unknown>(() => undefined),
    }) as Response)
    const controller = new AbortController()
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    const pending = transport.requestJson('/api/hanging-error-abort', {
      signal: controller.signal,
      timeoutMs: 0,
    })
    const assertion = expect(pending).rejects.toMatchObject({ kind: 'aborted' })
    await Promise.resolve()
    controller.abort('route-left')

    await assertion
    expect(cancelCalled).toBe(true)
  })

  it('cancels an unreadable 4xx body before returning its status error', async () => {
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: false,
        status: 502,
        headers: new Headers({ 'content-type': 'application/json' }),
        body,
        json: () => { throw new Error('malformed body') },
      }) as unknown as Response),
    })

    await expect(transport.requestJson('/api/unreadable-error')).rejects.toMatchObject({
      kind: 'http-status',
      status: 502,
    })
    expect(cancelCalled).toBe(true)
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

  it('rejects a JSON value that resolves after the transport timed out', async () => {
    vi.useFakeTimers()
    let resolveJson!: (value: unknown) => void
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: () => new Promise<unknown>(resolve => { resolveJson = resolve }),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 5,
    })

    const pending = transport.requestJson('/api/late-json')
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(5)
    resolveJson({ late: true })

    await expect(pending).rejects.toMatchObject({ kind: 'timeout' })
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

  it('requires an explicit non-GET method for request bodies at type and runtime boundaries', async () => {
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(),
    })

    if (false) {
      // @ts-expect-error body-bearing requests require an explicit method
      void transport.requestJson('/api/value', { json: { value: true } })
      // @ts-expect-error GET cannot carry a JSON body
      void transport.requestJson('/api/value', { method: 'GET', json: { value: true } })
    }
    await expect(transport.requestJson('/api/value', {
      method: 'GET',
      json: { value: true },
    } as never)).rejects.toMatchObject({ kind: 'encode' })
  })

  it('rejects invalid runtime methods and ambiguous or invalid body shapes', async () => {
    const fetchMock = vi.fn()
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
    })

    for (const requestOptions of [
      { method: null },
      { method: 'get', json: { value: true } },
      { method: 'HEAD' },
      { method: 'TRACE' },
      { method: 'GET', form: new URLSearchParams('value=true') },
      { method: 'POST', json: {}, form: new FormData() },
      { method: 'POST', form: undefined },
      { method: 'POST', json: undefined },
      { method: 'POST', form: Object.create(FormData.prototype) },
      { method: 'POST', form: { [Symbol.toStringTag]: 'FormData' } },
      {
        method: 'POST',
        form: Object.create({
          has: () => false,
          [Symbol.toStringTag]: 'FormData',
        }),
      },
      {
        method: 'POST',
        form: Object.create({
          toString: () => '',
          [Symbol.toStringTag]: 'URLSearchParams',
        }),
      },
    ]) {
      await expect(transport.requestJson('/api/value', requestOptions as never))
        .rejects.toMatchObject({ kind: 'encode' })
    }
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('normalizes auth-source and header construction failures', async () => {
    const fetchMock = vi.fn()
    const sourceFailure = new Error('credential source unavailable')
    const throwingAuth = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      authToken: () => { throw sourceFailure },
      fetch: fetchMock,
    })
    await expect(throwingAuth.requestJson('/api/value')).rejects.toMatchObject({
      kind: 'encode',
      transportCause: sourceFailure,
    })

    const invalidHeader = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      authToken: () => 'token\r\ninjected: value',
      fetch: fetchMock,
    })
    await expect(invalidHeader.requestJson('/api/value')).rejects.toMatchObject({
      kind: 'encode',
    })
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

  it('rechecks timeout after a successful decoder resolves late', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: () => new Promise<unknown>(resolve => {
        globalThis.setTimeout(() => resolve({ late: true }), 20)
      }),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 10,
    })

    const decoding = transport.requestJson('/api/late-body')
    const assertion = expect(decoding).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(20)
    await assertion
  })

  it('rechecks caller cancellation after a successful decoder resolves late', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: () => new Promise<unknown>(resolve => {
        globalThis.setTimeout(() => resolve({ late: true }), 20)
      }),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 0,
    })

    const decoding = transport.requestJson('/api/cancelled-body', {
      signal: controller.signal,
    })
    const assertion = expect(decoding).rejects.toMatchObject({ kind: 'aborted' })
    controller.abort('route-left')
    await vi.advanceTimersByTimeAsync(20)
    await assertion
  })

  it('keeps timeout classification active while a binary stream is consumed', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/octet-stream' }),
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener('abort', () => {
            controller.error(new DOMException('aborted', 'AbortError'))
          }, { once: true })
        },
      }),
    }) as Response)
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: fetchMock,
      defaultTimeoutMs: 10,
    })

    const blob = transport.requestBlob('/api/slow-binary')
    const assertion = expect(blob).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(10)
    await assertion
  })

  it('does not publish buffered binary bytes after timeout or caller abort', async () => {
    vi.useFakeTimers()
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => new Response('buffered-bytes')),
      defaultTimeoutMs: 5,
    })

    const timedOutBinary = await transport.requestBinary('/api/buffered')
    await vi.advanceTimersByTimeAsync(5)
    expect(() => timedOutBinary.stream()).toThrowError(HttpTransportError)
    await expect(timedOutBinary.blob()).rejects.toMatchObject({ kind: 'timeout' })

    const controller = new AbortController()
    const abortedBinary = await transport.requestBinary('/api/aborted', {
      signal: controller.signal,
      timeoutMs: 0,
    })
    controller.abort('route-left')
    expect(() => abortedBinary.stream()).toThrowError(HttpTransportError)
    await expect(abortedBinary.blob()).rejects.toMatchObject({ kind: 'aborted' })
  })

  it('cancels an unconsumed binary body when the caller aborts', async () => {
    let cancelCalled = false
    const source = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const controller = new AbortController()
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: source,
      }) as Response),
    })

    const binary = await transport.requestBinary('/api/unconsumed-abort', {
      signal: controller.signal,
      timeoutMs: 0,
    })
    controller.abort('route-left')
    await Promise.resolve()

    expect(cancelCalled).toBe(true)
    await expect(binary.blob()).rejects.toMatchObject({ kind: 'aborted' })
  })

  it('cancels a binary body when blob wrapping fails', async () => {
    let cancelCalled = false
    const source = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: source,
      }) as Response),
    })
    const binary = await transport.requestBinary('/api/blob-failure')
    const blobSpy = vi.spyOn(Response.prototype, 'blob').mockRejectedValue(
      new Error('blob wrapper failed'),
    )

    try {
      await expect(binary.blob()).rejects.toMatchObject({
        kind: 'network',
        status: 200,
      })
      expect(cancelCalled).toBe(true)
    } finally {
      blobSpy.mockRestore()
    }
  })

  it('aborts a hanging blob wrapper and cancels its source on timeout', async () => {
    vi.useFakeTimers()
    let cancelCalled = false
    const source = new ReadableStream<Uint8Array>({
      cancel() {
        cancelCalled = true
      },
    })
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: source,
      }) as Response),
      defaultTimeoutMs: 10,
    })
    const binary = await transport.requestBinary('/api/blob-timeout')
    const blobSpy = vi.spyOn(Response.prototype, 'blob').mockImplementation(
      () => new Promise<Blob>(() => undefined),
    )

    try {
      const pending = binary.blob()
      const assertion = expect(pending).rejects.toMatchObject({ kind: 'timeout' })
      await vi.advanceTimersByTimeAsync(10)
      await assertion
      expect(cancelCalled).toBe(true)
    } finally {
      blobSpy.mockRestore()
    }
  })

  it('cancels a binary body when stream acquisition fails', async () => {
    const cancel = vi.fn()
    const source = {
      getReader() {
        throw new Error('reader unavailable')
      },
      cancel,
    } as unknown as ReadableStream<Uint8Array>
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: source,
      }) as Response),
    })
    const binary = await transport.requestBinary('/api/stream-failure')

    expect(() => binary.stream()).toThrowError(HttpTransportError)
    expect(cancel).toHaveBeenCalled()
  })

  it('releases the underlying binary reader after end-of-stream', async () => {
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('bytes'))
        controller.close()
      },
    })
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: source,
      }) as Response),
    })

    const binary = await transport.requestBinary('/api/reader-release')
    const stream = binary.stream()
    expect(stream).toBeInstanceOf(ReadableStream)
    await expect(new Response(stream).text()).resolves.toBe('bytes')
    expect(() => source.getReader()).not.toThrow()
  })

  it('fails a binary read when timeout wins while the body read is pending', async () => {
    vi.useFakeTimers()
    let resolveRead: ((result: ReadableStreamReadResult<Uint8Array>) => void) | undefined
    let cancelCalled = false
    const reader = {
      read: () => new Promise<ReadableStreamReadResult<Uint8Array>>(resolve => {
        resolveRead = resolve
      }),
      cancel: async () => {
        cancelCalled = true
      },
      releaseLock: vi.fn(),
    }
    const body = { getReader: () => reader } as unknown as ReadableStream<Uint8Array>
    const transport = createPrivateHttpTransport({
      baseUrl: 'https://control.example/',
      fetch: vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body,
      }) as Response),
      defaultTimeoutMs: 10,
    })

    const binary = await transport.requestBinary('/api/pending-timeout')
    const stream = binary.stream()
    expect(stream).toBeInstanceOf(ReadableStream)
    await vi.advanceTimersByTimeAsync(10)
    resolveRead?.({ done: false, value: new Uint8Array([1]) })

    await expect(new Response(stream).arrayBuffer()).rejects.toMatchObject({
      kind: 'timeout',
    })
    expect(cancelCalled).toBe(true)
    expect(reader.releaseLock).toHaveBeenCalled()
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
