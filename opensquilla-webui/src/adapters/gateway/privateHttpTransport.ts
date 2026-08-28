const DEFAULT_TIMEOUT_MS = 15_000
const WS_TOKEN_KEY = 'opensquilla.wsToken'

export type HttpTransportErrorKind =
  | 'invalid-endpoint'
  | 'aborted'
  | 'timeout'
  | 'network'
  | 'http-status'
  | 'encode'
  | 'decode'

/**
 * Stable error emitted by the private HTTP seam.
 *
 * Gateway Adapters translate this transport detail into their domain errors;
 * Vue and domain Modules must never receive a native Response or fetch error.
 */
export class HttpTransportError extends Error {
  readonly name = 'HttpTransportError'

  constructor(
    readonly kind: HttpTransportErrorKind,
    message: string,
    readonly status?: number,
    readonly payload?: unknown,
    readonly transportCause?: unknown,
  ) {
    super(message)
  }
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

interface HttpRequestBase {
  sessionKey?: string
  timeoutMs?: number
  signal?: AbortSignal
}

type HttpBodyMethod = Exclude<HttpMethod, 'GET'>

export type HttpRequestOptions = HttpRequestBase & (
  | { method?: HttpMethod; json?: never; form?: never }
  | { method: HttpBodyMethod; json: unknown; form?: never }
  | { method: HttpBodyMethod; json?: never; form: FormData | URLSearchParams }
)

/** Raw HTTP capability private to Gateway Adapters and the composition root. */
export interface HttpTransport {
  requestJson<T>(endpoint: string, options?: HttpRequestOptions): Promise<T>
  requestBinary(endpoint: string, options?: HttpRequestOptions): Promise<HttpBinaryResponse>
  requestBlob(endpoint: string, options?: HttpRequestOptions): Promise<Blob>
}

export interface HttpBinaryMetadata {
  readonly status: number
  readonly filename?: string
  readonly contentLength?: number
  readonly contentType?: string
}

/** One-shot binary body without exposing native Response or Headers. */
export interface HttpBinaryResponse {
  readonly metadata: HttpBinaryMetadata
  blob(): Promise<Blob>
  stream(): ReadableStream<Uint8Array> | null
}

interface PrivateHttpTransportOptions {
  baseUrl?: string | URL
  authToken?: () => string | null | undefined
  fetch?: typeof globalThis.fetch
  defaultTimeoutMs?: number
}

interface ResponseDecoder<T> {
  decode(response: Response, lifecycle: ResponseBodyLifecycle): Promise<T>
  readonly failureMessage: string
  readonly ownsBodyLifecycle?: boolean
}

interface ResponseBodyLifecycle {
  release(): void
  assertActive(): void
  transportError(cause: unknown): HttpTransportError
}

function defaultAuthToken(): string {
  try {
    return globalThis.sessionStorage?.getItem(WS_TOKEN_KEY)?.trim() ?? ''
  } catch {
    return ''
  }
}

function resolveBaseUrl(value?: string | URL): URL {
  try {
    if (value instanceof URL) return new URL(value.href)
    if (value) return new URL(value, globalThis.location?.href)
    if (globalThis.location?.href) return new URL(globalThis.location.href)
  } catch (cause) {
    throw new HttpTransportError(
      'invalid-endpoint',
      'Gateway HTTP base URL is invalid.',
      undefined,
      undefined,
      cause,
    )
  }
  throw new HttpTransportError(
    'invalid-endpoint',
    'Gateway HTTP base URL is unavailable.',
  )
}

function resolveEndpoint(baseUrl: URL, endpoint: string): URL {
  const candidate = endpoint.trim()
  if (!candidate) {
    throw new HttpTransportError('invalid-endpoint', 'Gateway HTTP endpoint is empty.')
  }
  let resolved: URL
  try {
    resolved = new URL(candidate, baseUrl)
  } catch (cause) {
    throw new HttpTransportError(
      'invalid-endpoint',
      'Gateway HTTP endpoint is invalid.',
      undefined,
      undefined,
      cause,
    )
  }
  if (
    (resolved.protocol !== 'http:' && resolved.protocol !== 'https:')
    || resolved.origin !== baseUrl.origin
    || resolved.username
    || resolved.password
  ) {
    throw new HttpTransportError(
      'invalid-endpoint',
      'Gateway HTTP endpoint must stay on the configured origin.',
    )
  }
  return resolved
}

function requestTimeout(value: number | undefined, fallback: number): number {
  const timeoutMs = value ?? fallback
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) {
    throw new HttpTransportError(
      'invalid-endpoint',
      'Gateway HTTP timeout must be a finite non-negative number.',
    )
  }
  return timeoutMs
}

interface RequestSignal {
  readonly signal: AbortSignal
  readonly timedOut: () => boolean
  dispose(): void
}

function requestSignal(signal: AbortSignal | undefined, timeoutMs: number): RequestSignal {
  const controller = new AbortController()
  let didTimeOut = false
  let disposed = false
  let timeout: ReturnType<typeof globalThis.setTimeout> | undefined
  const dispose = () => {
    if (disposed) return
    disposed = true
    if (timeout !== undefined) globalThis.clearTimeout(timeout)
    signal?.removeEventListener('abort', abortFromCaller)
  }
  const abortFromCaller = () => {
    controller.abort(signal?.reason)
    dispose()
  }
  if (signal?.aborted) abortFromCaller()
  else signal?.addEventListener('abort', abortFromCaller, { once: true })

  timeout = !disposed && timeoutMs > 0
    ? globalThis.setTimeout(() => {
      didTimeOut = true
      controller.abort()
      dispose()
    }, timeoutMs)
    : undefined

  return {
    signal: controller.signal,
    timedOut: () => didTimeOut,
    dispose,
  }
}

function binaryContentLength(value: string | null): number | undefined {
  if (value === null || !/^\d+$/.test(value.trim())) return undefined
  const length = Number(value)
  return Number.isSafeInteger(length) && length >= 0 ? length : undefined
}

function binaryFilename(value: string | null): string | undefined {
  if (!value) return undefined
  const encoded = /(?:^|;)\s*filename\s*\*\s*=\s*([^;]*)/i.exec(value)?.[1]?.trim()
  let candidate: string | undefined
  const extended = encoded && /^([^']*)'[^']*'(.*)$/.exec(encoded)
  if (extended && /^(?:utf-?8)$/i.test(extended[1].trim())) {
    try {
      candidate = decodeURIComponent(extended[2])
    } catch {
      // Fall back to filename= when an invalid extended value is present.
    }
  }
  if (!candidate) {
    const plain = /(?:^|;)\s*filename\s*=\s*(?:"((?:\\.|[^"])*)"|([^;]*))/i.exec(value)
    candidate = plain
      ? (plain[1] ?? plain[2] ?? '').replace(/\\(["\\])/g, '$1').trim()
      : undefined
  }
  if (!candidate) return undefined
  const sanitized = candidate
    .replace(/^[A-Za-z]:[\\/]+/, '')
    .replace(/^[A-Za-z]:/, '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, '')
    .trim()
    .replace(/^\.+/, '')
    .replace(/[. ]+$/, '')
    .slice(0, 255)
  if (!sanitized) return undefined
  const windowsStem = sanitized.split('.')[0].replace(/[. ]+$/, '')
  return /^(?:con|prn|aux|nul|com(?:[1-9]|[¹²³])|lpt(?:[1-9]|[¹²³])|conin\$|conout\$|clock\$)$/i.test(windowsStem)
    ? `_${sanitized}`.slice(0, 255)
    : sanitized
}

function isHttpMethod(value: unknown): value is HttpMethod {
  return value === 'GET'
    || value === 'POST'
    || value === 'PUT'
    || value === 'PATCH'
    || value === 'DELETE'
}

function isFormBody(value: unknown): value is FormData | URLSearchParams {
  if (value === null || typeof value !== 'object') return false
  try {
    const ownPrototype = Object.getPrototypeOf(value)
    const tag = Object.prototype.toString.call(value)
    if (typeof FormData === 'function' && FormData.prototype) {
      const prototypes = [FormData.prototype]
      if (tag === '[object FormData]' && ownPrototype && ownPrototype !== Object.prototype) {
        prototypes.push(ownPrototype as typeof FormData.prototype)
      }
      for (const prototype of prototypes) {
        const has = prototype.has
        if (typeof has !== 'function') continue
        try {
          has.call(value, '')
          return true
        } catch {
          // Try the next realm's prototype or URLSearchParams below.
        }
      }
    }
    if (typeof URLSearchParams === 'function' && URLSearchParams.prototype) {
      const prototypes = [URLSearchParams.prototype]
      if (tag === '[object URLSearchParams]' && ownPrototype && ownPrototype !== Object.prototype) {
        prototypes.push(ownPrototype as typeof URLSearchParams.prototype)
      }
      for (const prototype of prototypes) {
        const toString = prototype.toString
        if (typeof toString !== 'function') continue
        try {
          toString.call(value)
          return true
        } catch {
          // A spoofed prototype/tag is not a supported form body.
        }
      }
    }
  } catch {
    // Proxies and malformed objects are not supported form bodies.
  }
  return false
}

function managedBinaryStream(
  source: ReadableStream<Uint8Array>,
  lifecycle: ResponseBodyLifecycle,
): ReadableStream<Uint8Array> {
  const reader = source.getReader()
  let readerReleased = false
  let readerCancelPromise: Promise<void> | undefined
  function releaseReader(): void {
    if (readerReleased) return
    readerReleased = true
    reader.releaseLock()
  }
  async function cancelReader(reason: unknown): Promise<void> {
    if (!readerCancelPromise) {
      readerCancelPromise = reader.cancel(reason).then(
        () => {
          releaseReader()
        },
        cause => {
          releaseReader()
          throw cause
        },
      )
    }
    return readerCancelPromise
  }
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        lifecycle.assertActive()
        const next = await reader.read()
        lifecycle.assertActive()
        if (next.done) {
          controller.close()
          releaseReader()
          lifecycle.release()
        } else {
          controller.enqueue(next.value)
        }
      } catch (cause) {
        const error = cause instanceof HttpTransportError
          ? cause
          : lifecycle.transportError(cause)
        controller.error(error)
        try {
          await cancelReader(error)
        } catch {
          // The stream is already being failed; preserve the stable transport
          // error even if source cleanup rejects.
        }
        lifecycle.release()
      }
    },
    async cancel(reason) {
      try {
        await cancelReader(reason)
      } catch (cause) {
        if (cause instanceof HttpTransportError) throw cause
        throw lifecycle.transportError(cause)
      } finally {
        lifecycle.release()
      }
    },
  })
}

function binaryResponse(initialResponse: Response, lifecycle: ResponseBodyLifecycle): HttpBinaryResponse {
  let response: Response | null = initialResponse
  const status = response.status
  const contentType = response.headers.get('content-type')?.trim() || undefined
  const filename = binaryFilename(response.headers.get('content-disposition'))
  const contentLength = binaryContentLength(response.headers.get('content-length'))
  const metadata: HttpBinaryMetadata = {
    status,
    ...(filename ? { filename } : {}),
    ...(contentLength !== undefined ? { contentLength } : {}),
    ...(contentType ? { contentType } : {}),
  }
  let body = response.body
  response = null
  let consumed = false
  function discardBody(reason: unknown, sourceOverride?: ReadableStream<Uint8Array> | null): void {
    const source = sourceOverride === undefined ? body : sourceOverride
    body = null
    if (source) {
      try {
        void source.cancel(reason).catch(() => undefined)
      } catch {
        // A body that is already disturbed/closed needs no further cleanup.
      }
    }
    lifecycle.release()
  }
  function takeStream(): ReadableStream<Uint8Array> | null {
    try {
      lifecycle.assertActive()
    } catch (cause) {
      discardBody(cause)
      throw cause
    }
    if (consumed) {
      throw new HttpTransportError(
        'decode',
        'Gateway HTTP binary response has already been consumed.',
        status,
      )
    }
    consumed = true
    const source = body
    body = null
    if (!source) {
      lifecycle.release()
      return null
    }
    try {
      return managedBinaryStream(source, lifecycle)
    } catch (cause) {
      discardBody(cause, source)
      if (cause instanceof HttpTransportError) throw cause
      throw lifecycle.transportError(cause)
    }
  }
  return {
    metadata,
    stream: takeStream,
    async blob() {
      const stream = takeStream()
      if (!stream) return new Blob([], contentType ? { type: contentType } : undefined)
      try {
        return await new Response(
          stream,
          contentType ? { headers: { 'content-type': contentType } } : undefined,
        ).blob()
      } catch (cause) {
        if (cause instanceof HttpTransportError) throw cause
        throw lifecycle.transportError(cause)
      }
    },
  }
}

async function errorPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
  try {
    if (contentType.includes('json')) return await response.json()
    const text = await response.text()
    return text || undefined
  } catch {
    return undefined
  }
}

/**
 * Create the single same-origin HTTP implementation used by Gateway Adapters.
 * The injected fetch/token sources are internal seams for deterministic tests.
 */
export function createPrivateHttpTransport(
  options: PrivateHttpTransportOptions = {},
): HttpTransport {
  const baseUrl = resolveBaseUrl(options.baseUrl)
  const fetchImpl = options.fetch ?? globalThis.fetch?.bind(globalThis)
  if (!fetchImpl) {
    throw new HttpTransportError('network', 'Gateway HTTP transport is unavailable.')
  }
  const authToken = options.authToken ?? defaultAuthToken
  const defaultTimeoutMs = requestTimeout(options.defaultTimeoutMs, DEFAULT_TIMEOUT_MS)

  async function request<T>(
    endpoint: string,
    requestOptions: HttpRequestOptions = {},
    decoder: ResponseDecoder<T>,
  ): Promise<T> {
    const url = resolveEndpoint(baseUrl, endpoint)
    let method: HttpMethod = 'GET'
    let hasJson = false
    let hasForm = false
    let body: BodyInit | undefined
    try {
      if (requestOptions === null || typeof requestOptions !== 'object') {
        throw new HttpTransportError('encode', 'Gateway HTTP request options are invalid.')
      }
      const requestedMethod = requestOptions.method ?? 'GET'
      if (!isHttpMethod(requestedMethod)) {
        throw new HttpTransportError('encode', 'Gateway HTTP request method is invalid.')
      }
      method = requestedMethod
      hasJson = Object.prototype.hasOwnProperty.call(requestOptions, 'json')
      hasForm = Object.prototype.hasOwnProperty.call(requestOptions, 'form')
      if (hasJson && hasForm) {
        throw new HttpTransportError(
          'encode',
          'Gateway HTTP request cannot include both JSON and form bodies.',
        )
      }
      if ((hasJson || hasForm) && method === 'GET') {
        throw new HttpTransportError(
          'encode',
          'Gateway HTTP GET requests cannot include a body.',
        )
      }
      if (hasJson) {
        try {
          body = JSON.stringify(requestOptions.json)
        } catch (cause) {
          throw new HttpTransportError(
            'encode',
            'Gateway HTTP request could not be encoded as JSON.',
            undefined,
            undefined,
            cause,
          )
        }
        if (body === undefined) {
          throw new HttpTransportError(
            'encode',
            'Gateway HTTP request could not be encoded as JSON.',
          )
        }
      } else if (hasForm) {
        const form = requestOptions.form
        if (!isFormBody(form)) {
          throw new HttpTransportError('encode', 'Gateway HTTP form body is invalid.')
        }
        body = form
      }
    } catch (cause) {
      if (cause instanceof HttpTransportError) throw cause
      throw new HttpTransportError(
        'encode',
        'Gateway HTTP request options could not be encoded.',
        undefined,
        undefined,
        cause,
      )
    }

    let headers: Headers
    try {
      headers = new Headers()
      const token = authToken()?.trim() ?? ''
      if (token) headers.set('Authorization', `Bearer ${token}`)
      const sessionKey = requestOptions.sessionKey?.trim() ?? ''
      if (sessionKey) headers.set('x-opensquilla-session-key', sessionKey)
      if (hasJson) headers.set('Content-Type', 'application/json')
    } catch (cause) {
      throw new HttpTransportError(
        'encode',
        'Gateway HTTP request headers could not be constructed.',
        undefined,
        undefined,
        cause,
      )
    }

    const linkedSignal = requestSignal(
      requestOptions.signal,
      requestTimeout(requestOptions.timeoutMs, defaultTimeoutMs),
    )
    let lifecycleTransferred = false
    try {
      let response: Response
      try {
        response = await fetchImpl(url, {
          method,
          headers,
          body,
          credentials: 'same-origin',
          redirect: 'error',
          signal: linkedSignal.signal,
        })
      } catch (cause) {
        if (linkedSignal.timedOut()) {
          throw new HttpTransportError(
            'timeout',
            'Gateway HTTP request timed out.',
            undefined,
            undefined,
            cause,
          )
        }
        if (requestOptions.signal?.aborted || linkedSignal.signal.aborted) {
          throw new HttpTransportError(
            'aborted',
            'Gateway HTTP request was aborted.',
            undefined,
            undefined,
            cause,
          )
        }
        throw new HttpTransportError(
          'network',
          'Gateway HTTP request failed.',
          undefined,
          undefined,
          cause,
        )
      }

      if (!response.ok) {
        const payload = await errorPayload(response)
        if (linkedSignal.timedOut()) {
          throw new HttpTransportError('timeout', 'Gateway HTTP request timed out.')
        }
        if (requestOptions.signal?.aborted || linkedSignal.signal.aborted) {
          throw new HttpTransportError('aborted', 'Gateway HTTP request was aborted.')
        }
        throw new HttpTransportError(
          'http-status',
          `Gateway HTTP request failed with status ${response.status}.`,
          response.status,
          payload,
        )
      }

      const responseStatus = response.status
      try {
        const callerSignal = requestOptions.signal
        const lifecycle: ResponseBodyLifecycle = {
          release: () => linkedSignal.dispose(),
          assertActive() {
            if (linkedSignal.timedOut()) {
              throw new HttpTransportError('timeout', 'Gateway HTTP request timed out.')
            }
            if (callerSignal?.aborted || linkedSignal.signal.aborted) {
              throw new HttpTransportError('aborted', 'Gateway HTTP request was aborted.')
            }
          },
          transportError(cause) {
            if (cause instanceof HttpTransportError) return cause
            if (linkedSignal.timedOut()) {
              return new HttpTransportError(
                'timeout',
                'Gateway HTTP request timed out.',
                undefined,
                undefined,
                cause,
              )
            }
            if (callerSignal?.aborted || linkedSignal.signal.aborted) {
              return new HttpTransportError(
                'aborted',
                'Gateway HTTP request was aborted.',
                undefined,
                undefined,
                cause,
              )
            }
            return new HttpTransportError(
              'network',
              'Gateway HTTP response body failed.',
              responseStatus,
              undefined,
              cause,
            )
          },
        }
        const decoded = await decoder.decode(response, lifecycle)
        lifecycle.assertActive()
        lifecycleTransferred = decoder.ownsBodyLifecycle === true
        return decoded
      } catch (cause) {
        if (linkedSignal.timedOut()) {
          throw new HttpTransportError(
            'timeout',
            'Gateway HTTP request timed out.',
            undefined,
            undefined,
            cause,
          )
        }
        if (requestOptions.signal?.aborted || linkedSignal.signal.aborted) {
          throw new HttpTransportError(
            'aborted',
            'Gateway HTTP request was aborted.',
            undefined,
            undefined,
            cause,
          )
        }
        throw new HttpTransportError(
          'decode',
          decoder.failureMessage,
          responseStatus,
          undefined,
          cause,
        )
      }
    } finally {
      if (!lifecycleTransferred) linkedSignal.dispose()
    }
  }

  const requestBinary = async (
    endpoint: string,
    requestOptions?: HttpRequestOptions,
  ): Promise<HttpBinaryResponse> => request(endpoint, requestOptions, {
    decode: async (response, lifecycle) => binaryResponse(response, lifecycle),
    failureMessage: 'Gateway HTTP response could not be decoded as binary data.',
    ownsBodyLifecycle: true,
  })

  return {
    async requestJson<T>(endpoint: string, requestOptions?: HttpRequestOptions) {
      return request(endpoint, requestOptions, {
        async decode(response) {
          if (response.status === 204 || response.status === 205) return undefined as T
          return await response.json() as T
        },
        failureMessage: 'Gateway HTTP response is not valid JSON.',
      })
    },
    requestBinary,
    async requestBlob(endpoint: string, requestOptions?: HttpRequestOptions) {
      const binary = await requestBinary(endpoint, requestOptions)
      return binary.blob()
    },
  }
}
