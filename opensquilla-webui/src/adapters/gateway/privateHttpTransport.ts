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
  method?: HttpMethod
  sessionKey?: string
  timeoutMs?: number
  signal?: AbortSignal
}

type HttpRequestContent =
  | { json?: never; form?: never }
  | { json: unknown; form?: never }
  | { json?: never; form: FormData | URLSearchParams }

export type HttpRequestOptions = HttpRequestBase & HttpRequestContent

/** Raw HTTP capability private to Gateway Adapters and the composition root. */
export interface HttpTransport {
  requestJson<T>(endpoint: string, options?: HttpRequestOptions): Promise<T>
  requestBlob(endpoint: string, options?: HttpRequestOptions): Promise<Blob>
}

interface PrivateHttpTransportOptions {
  baseUrl?: string | URL
  authToken?: () => string | null | undefined
  fetch?: typeof globalThis.fetch
  defaultTimeoutMs?: number
}

interface ResponseDecoder<T> {
  decode(response: Response): Promise<T>
  readonly failureMessage: string
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
  const abortFromCaller = () => controller.abort(signal?.reason)
  if (signal?.aborted) abortFromCaller()
  else signal?.addEventListener('abort', abortFromCaller, { once: true })

  const timeout = timeoutMs > 0
    ? globalThis.setTimeout(() => {
      didTimeOut = true
      controller.abort()
    }, timeoutMs)
    : undefined

  return {
    signal: controller.signal,
    timedOut: () => didTimeOut,
    dispose() {
      if (timeout !== undefined) globalThis.clearTimeout(timeout)
      signal?.removeEventListener('abort', abortFromCaller)
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
    const headers = new Headers()
    const token = authToken()?.trim() ?? ''
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const sessionKey = requestOptions.sessionKey?.trim() ?? ''
    if (sessionKey) headers.set('x-opensquilla-session-key', sessionKey)

    let body: BodyInit | undefined
    if ('json' in requestOptions) {
      headers.set('Content-Type', 'application/json')
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
    } else if ('form' in requestOptions) {
      body = requestOptions.form
    }

    const linkedSignal = requestSignal(
      requestOptions.signal,
      requestTimeout(requestOptions.timeoutMs, defaultTimeoutMs),
    )
    try {
      let response: Response
      try {
        response = await fetchImpl(url, {
          method: requestOptions.method ?? 'GET',
          headers,
          body,
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

      try {
        return await decoder.decode(response)
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
          response.status,
          undefined,
          cause,
        )
      }
    } finally {
      linkedSignal.dispose()
    }
  }

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
    async requestBlob(endpoint: string, requestOptions?: HttpRequestOptions) {
      return request(endpoint, requestOptions, {
        decode: response => response.blob(),
        failureMessage: 'Gateway HTTP response could not be decoded as a blob.',
      })
    },
  }
}
