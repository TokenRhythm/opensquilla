const DEFAULT_TIMEOUT_MS = 15_000
const PREVIEW_HOSTNAME_PATTERN = /^p-[0-9a-f]{32}\.localhost$/
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
  keepalive?: boolean
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
  clearPreviewOrigin(previewOrigin: string): Promise<void>
  fetchExternalArtifact(endpoint: string, signal?: AbortSignal): Promise<HttpBinaryResponse>
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
  readonly signal: AbortSignal
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

function invalidEndpoint(message: string, cause?: unknown): HttpTransportError {
  return new HttpTransportError('invalid-endpoint', message, undefined, undefined, cause)
}
function resolveBaseUrl(value?: string | URL): URL {
  try {
    if (value instanceof URL) return new URL(value.href)
    if (value) return new URL(value, globalThis.location?.href)
    if (globalThis.location?.href) return new URL(globalThis.location.href)
  } catch (cause) {
    throw invalidEndpoint('Gateway HTTP base URL is invalid.', cause)
  }
  throw invalidEndpoint('Gateway HTTP base URL is unavailable.')
}

function isDesktopGatewayUrl(url: URL): boolean {
  return url.protocol === 'opensquilla-app:' && url.hostname === 'desktop'
    && !(url.port || url.username || url.password)
}

function resolveEndpoint(baseUrl: URL, endpoint: string): URL {
  if (typeof endpoint !== 'string') throw invalidEndpoint('Gateway HTTP endpoint is invalid.')
  const candidate = endpoint.trim()
  if (!candidate) throw invalidEndpoint('Gateway HTTP endpoint is empty.')
  let resolved: URL
  try {
    resolved = new URL(candidate, baseUrl)
  } catch (cause) {
    throw invalidEndpoint('Gateway HTTP endpoint is invalid.', cause)
  }
  // Opaque origins cannot establish authority: compare the Desktop fields explicitly.
  const sameGateway = isDesktopGatewayUrl(baseUrl) && isDesktopGatewayUrl(resolved)
    ? resolved.pathname === '/api' || resolved.pathname.startsWith('/api/')
    : (resolved.protocol === 'http:' || resolved.protocol === 'https:')
      && resolved.origin === baseUrl.origin
  if (!sameGateway || resolved.username || resolved.password) {
    throw invalidEndpoint('Gateway HTTP endpoint must stay on the configured origin.')
  }
  return resolved
}

function resolvePreviewCleanupUrl(previewOrigin: string): URL {
  const origin = resolveBaseUrl(previewOrigin)
  if (
    origin.protocol !== 'http:' || !PREVIEW_HOSTNAME_PATTERN.test(origin.hostname) || !origin.port
    || !!(origin.username || origin.password)
    || origin.pathname !== '/' || !!(origin.search || origin.hash)
    || previewOrigin !== origin.origin
  ) {
    throw invalidEndpoint('Preview cleanup origin is invalid.')
  }
  return new URL('/.opensquilla/clear-site-data', origin.origin)
}

function resolveExternalArtifactUrl(baseUrl: URL, endpoint: string): URL {
  let resolved: URL
  try { resolved = new URL(endpoint) } catch (cause) {
    throw invalidEndpoint('External artifact endpoint is invalid.', cause)
  }
  if (
    (resolved.protocol !== 'http:' && resolved.protocol !== 'https:')
    || resolved.origin === baseUrl.origin || !!(resolved.username || resolved.password)
  ) throw invalidEndpoint('External artifact endpoint is invalid.')
  return resolved
}

function requestTimeout(value: number | undefined, fallback: number): number {
  const timeoutMs = value ?? fallback
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw invalidEndpoint('Gateway HTTP timeout must be finite and non-negative.')
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

function requestFailure(
  linkedSignal: RequestSignal,
  callerSignal: AbortSignal | undefined,
  cause: unknown,
  kind: 'network' | 'decode',
  message: string,
  status?: number,
): HttpTransportError {
  if (linkedSignal.timedOut()) {
    return new HttpTransportError('timeout', 'Gateway HTTP request timed out.', undefined, undefined, cause)
  }
  if (callerSignal?.aborted || linkedSignal.signal.aborted) {
    return new HttpTransportError('aborted', 'Gateway HTTP request was aborted.', undefined, undefined, cause)
  }
  return new HttpTransportError(kind, message, status, undefined, cause)
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
    // Native internal-slot checks reject spoofed prototypes and toStringTag values.
    // Resolve each constructor lazily so a successful FormData check needs no fallback.
    for (const name of ['FormData', 'URLSearchParams'] as const) {
      const constructor = globalThis[name]
      if (typeof constructor !== 'function') continue
      const get = constructor.prototype?.get
      if (typeof get !== 'function') continue
      try {
        get.call(value, '')
        return true
      } catch {
        // Not this native form type; try the remaining brand.
      }
    }
  } catch {
    // Proxies and malformed objects are not supported form bodies.
  }
  return false
}

function cancelReadableStream(
  source: ReadableStream<Uint8Array> | null | undefined,
  reason: unknown,
): void {
  if (!source) return
  try {
    void Promise.resolve(source.cancel(reason)).catch(() => undefined)
  } catch {
    // A body that is already disturbed, locked, or closed needs no further
    // cleanup from this owner.
  }
}

function lifecycleAbortError(lifecycle: ResponseBodyLifecycle): HttpTransportError {
  try {
    lifecycle.assertActive()
  } catch (cause) {
    if (cause instanceof HttpTransportError) return cause
  }
  return new HttpTransportError('aborted', 'Gateway HTTP request was aborted.')
}

async function awaitWithLifecycle<T>(
  operation: PromiseLike<T>,
  lifecycle: ResponseBodyLifecycle,
): Promise<T> {
  let removeAbortListener: (() => void) | undefined
  const abortPromise = new Promise<never>((_, reject) => {
    const onAbort = () => reject(lifecycleAbortError(lifecycle))
    removeAbortListener = () => lifecycle.signal.removeEventListener('abort', onAbort)
    lifecycle.signal.addEventListener('abort', onAbort, { once: true })
    if (lifecycle.signal.aborted) onAbort()
  })
  try {
    return await Promise.race([operation, abortPromise])
  } finally {
    removeAbortListener?.()
  }
}

function managedBinaryStream(
  source: ReadableStream<Uint8Array>,
  lifecycle: ResponseBodyLifecycle,
): ReadableStream<Uint8Array> {
  const reader = source.getReader()
  let readerReleased = false
  let readerCancelPromise: Promise<void> | undefined
  let signalListener: (() => void) | undefined
  let finished = false
  let outputController: ReadableStreamDefaultController<Uint8Array> | undefined
  let outputSettled = false
  function releaseReader(): void {
    if (readerReleased) return
    readerReleased = true
    try {
      reader.releaseLock()
    } catch {
      // The underlying stream may have released the lock while cancelling.
    }
  }
  async function cancelReader(reason: unknown): Promise<void> {
    if (!readerCancelPromise) {
      try {
        readerCancelPromise = Promise.resolve(reader.cancel(reason)).then(
          () => {
            releaseReader()
          },
          cause => {
            releaseReader()
            throw cause
          },
        )
      } catch (cause) {
        releaseReader()
        readerCancelPromise = Promise.reject(cause)
      }
    }
    return readerCancelPromise
  }
  function detachSignalListener(): void {
    if (!signalListener) return
    lifecycle.signal.removeEventListener('abort', signalListener)
    signalListener = undefined
  }
  function releaseLifecycle(): void {
    if (finished) return
    finished = true
    detachSignalListener()
    lifecycle.release()
  }
  function failOutput(error: unknown): void {
    if (outputSettled || !outputController) return
    try {
      outputController.error(error)
    } catch {
      // The consumer may have cancelled/closed the wrapper concurrently.
    }
    outputSettled = true
  }
  const abortSource = () => {
    const error = lifecycleAbortError(lifecycle)
    failOutput(error)
    releaseLifecycle()
    void cancelReader(error).catch(() => undefined)
  }
  return new ReadableStream<Uint8Array>({
    start(controller) {
      outputController = controller
      signalListener = abortSource
      lifecycle.signal.addEventListener('abort', signalListener, { once: true })
      if (lifecycle.signal.aborted) abortSource()
    },
    async pull(controller) {
      try {
        lifecycle.assertActive()
        const next = await reader.read()
        lifecycle.assertActive()
        if (next.done) {
          controller.close()
          outputSettled = true
          releaseReader()
          releaseLifecycle()
        } else {
          controller.enqueue(next.value)
        }
      } catch (cause) {
        const error = cause instanceof HttpTransportError
          ? cause
          : lifecycle.transportError(cause)
        failOutput(error)
        try {
          await cancelReader(error)
        } catch {
          // The stream is already being failed; preserve the stable transport
          // error even if source cleanup rejects.
        }
        releaseLifecycle()
      }
    },
    async cancel(reason) {
      try {
        await cancelReader(reason)
      } catch (cause) {
        if (cause instanceof HttpTransportError) throw cause
        throw lifecycle.transportError(cause)
      } finally {
        outputSettled = true
        releaseLifecycle()
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
  let signalListener: (() => void) | undefined
  function detachSignalListener(): void {
    if (!signalListener) return
    lifecycle.signal.removeEventListener('abort', signalListener)
    signalListener = undefined
  }
  function releaseLifecycle(): void {
    detachSignalListener()
    lifecycle.release()
  }
  function discardBody(reason: unknown, sourceOverride?: ReadableStream<Uint8Array> | null): void {
    const source = sourceOverride === undefined ? body : sourceOverride
    body = null
    detachSignalListener()
    cancelReadableStream(source, reason)
    lifecycle.release()
  }
  const abortBody = () => {
    discardBody(lifecycleAbortError(lifecycle))
  }
  signalListener = abortBody
  lifecycle.signal.addEventListener('abort', signalListener, { once: true })
  if (lifecycle.signal.aborted) abortBody()
  function takeStream(claimEmpty = true): ReadableStream<Uint8Array> | null {
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
    const source = body
    body = null
    detachSignalListener()
    if (!source) {
      consumed = claimEmpty
      releaseLifecycle()
      return null
    }
    consumed = true
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
    stream: () => takeStream(false),
    async blob() {
      const stream = takeStream()
      if (!stream) return new Blob([], contentType ? { type: contentType } : undefined)
      try {
        const blobResponse = new Response(
          stream,
          contentType ? { headers: { 'content-type': contentType } } : undefined,
        )
        return await awaitWithLifecycle(blobResponse.blob(), lifecycle)
      } catch (cause) {
        cancelReadableStream(stream, cause)
        releaseLifecycle()
        if (cause instanceof HttpTransportError) throw cause
        throw lifecycle.transportError(cause)
      }
    },
  }
}
async function errorPayload(
  response: Response,
  lifecycle: ResponseBodyLifecycle,
): Promise<unknown> {
  let contentType = ''
  try {
    contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
  } catch {
    // Treat malformed response metadata like an unreadable error body.
  }

  const payloadPromise = Promise.resolve().then(async () => {
    if (contentType.includes('json')) return await response.json()
    const text = await response.text()
    return text || undefined
  })
  let reason: unknown
  try {
    return await awaitWithLifecycle(payloadPromise, lifecycle)
  } catch (cause) {
    if (
      cause instanceof HttpTransportError
      && (cause.kind === 'timeout' || cause.kind === 'aborted')
    ) throw cause
    try {
      lifecycle.assertActive()
    } catch (activeCause) {
      throw activeCause
    }
    return undefined
  } finally {
    try {
      lifecycle.assertActive()
    } catch (activeCause) {
      reason = activeCause
    }
    let body: ReadableStream<Uint8Array> | null = null
    try {
      body = response.body
    } catch {
      // A malformed response body accessor cannot be cleaned up further.
    }
    cancelReadableStream(body, reason)
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
    externalArtifact = false,
  ): Promise<T> {
    const url = externalArtifact
      ? resolveExternalArtifactUrl(baseUrl, endpoint)
      : resolveEndpoint(baseUrl, endpoint)
    let method: HttpMethod = 'GET'
    let hasJson = false
    let hasForm = false
    let body: BodyInit | undefined
    let callerSignal: AbortSignal | undefined
    let timeoutValue: number | undefined
    try {
      if (requestOptions === null || typeof requestOptions !== 'object') {
        throw new HttpTransportError('encode', 'Gateway HTTP request options are invalid.')
      }
      callerSignal = requestOptions.signal
      timeoutValue = requestOptions.timeoutMs
      const requestedMethod = requestOptions.method
      if (requestedMethod !== undefined && !isHttpMethod(requestedMethod)) {
        throw new HttpTransportError('encode', 'Gateway HTTP request method is invalid.')
      }
      method = requestedMethod ?? 'GET'
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

    let headers: Headers | undefined
    if (!externalArtifact) {
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
    }

    const linkedSignal = requestSignal(
      callerSignal,
      requestTimeout(timeoutValue, defaultTimeoutMs),
    )
    let lifecycleTransferred = false
    try {
      let response: Response
      try {
        response = await fetchImpl(url, {
          method,
          headers,
          body,
          credentials: externalArtifact ? 'omit' : 'same-origin',
          keepalive: requestOptions.keepalive,
          redirect: 'error',
          signal: linkedSignal.signal,
        })
      } catch (cause) {
        throw requestFailure(
          linkedSignal, callerSignal, cause, 'network', 'Gateway HTTP request failed.',
        )
      }

        const responseStatus = response.status
      const lifecycle: ResponseBodyLifecycle = {
        signal: linkedSignal.signal,
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
          return requestFailure(
            linkedSignal,
            callerSignal,
            cause,
            'network',
            'Gateway HTTP response body failed.',
            responseStatus,
          )
        },
      }

      if (!response.ok) {
        const payload = await errorPayload(response, lifecycle)
        lifecycle.assertActive()
        throw new HttpTransportError(
          'http-status',
          `Gateway HTTP request failed with status ${responseStatus}.`,
          responseStatus,
          payload,
        )
      }

      try {
        const decoded = await decoder.decode(response, lifecycle)
        lifecycle.assertActive()
        lifecycleTransferred = decoder.ownsBodyLifecycle === true
        return decoded
      } catch (cause) {
        throw requestFailure(
          linkedSignal,
          callerSignal,
          cause,
          'decode',
          decoder.failureMessage,
          responseStatus,
        )
      }
    } finally {
      if (!lifecycleTransferred) linkedSignal.dispose()
    }
  }

  const requestBinary = async (
    endpoint: string,
    requestOptions?: HttpRequestOptions,
    externalArtifact = false,
  ): Promise<HttpBinaryResponse> => request(endpoint, requestOptions, {
    decode: async (response, lifecycle) => binaryResponse(response, lifecycle),
    failureMessage: 'Gateway HTTP response could not be decoded as binary data.',
    ownsBodyLifecycle: true,
  }, externalArtifact)

  return {
    async clearPreviewOrigin(previewOrigin: string) {
      const url = resolvePreviewCleanupUrl(previewOrigin)
      const linkedSignal = requestSignal(undefined, 2_000)
      try {
        try {
          await fetchImpl(url, {
            method: 'GET',
            cache: 'no-store',
            credentials: 'omit',
            keepalive: true,
            mode: 'no-cors',
            redirect: 'error',
            referrerPolicy: 'no-referrer',
            signal: linkedSignal.signal,
          })
        } catch (cause) {
          throw requestFailure(
            linkedSignal, undefined, cause, 'network', 'Preview origin cleanup failed.',
          )
        }
      } finally {
        linkedSignal.dispose()
      }
    },
    fetchExternalArtifact(endpoint: string, signal?: AbortSignal) {
      return requestBinary(endpoint, { signal, timeoutMs: 0 }, true)
    },
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
