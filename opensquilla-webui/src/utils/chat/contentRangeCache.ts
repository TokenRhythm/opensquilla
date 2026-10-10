export interface ContentRangeRef {
  readonly version: 1
  readonly sessionKey: string
  readonly sessionId: string
  readonly messageId: string
  readonly source?: 'active' | 'compacted'
  readonly view?: 'raw' | 'display'
  readonly byteLength?: number
  /** Stable storage revision or content digest, when the server provides one. */
  readonly revision?: string
  readonly sha256?: string
}

export interface ContentRangeCacheOptions {
  readonly fetcher?: typeof fetch
  readonly baseUrl?: string
  readonly chunkBytes?: number
  readonly maxBytes?: number
  readonly maxEntries?: number
}

const DEFAULT_CHUNK_BYTES = 256 * 1024
const DEFAULT_MAX_BYTES = 8 * 1024 * 1024
const DEFAULT_MAX_ENTRIES = 32

function keyFor(ref: ContentRangeRef, offset: number, limit: number): string {
  return [
    ref.sessionKey, ref.sessionId, ref.messageId, ref.source ?? '', ref.view ?? 'raw',
    String(ref.byteLength ?? ''), ref.revision ?? '', ref.sha256 ?? '', offset, limit,
  ].join('|')
}

function abortError(): DOMException {
  return new DOMException('The operation was aborted.', 'AbortError')
}

function encode(value: string): string {
  return encodeURIComponent(value)
}

/**
 * Bounded, shared range reads for history content.
 *
 * The cache stores only finite Uint8Array chunks and coalesces concurrent
 * identical requests. It deliberately has no whole-message accumulator; an
 * export consumer must process chunks as they arrive.
 */
export class ContentRangeCache {
  private readonly fetcher: typeof fetch
  private readonly baseUrl: string
  private readonly chunkBytes: number
  private readonly maxBytes: number
  private readonly maxEntries: number
  private readonly cache = new Map<string, Uint8Array>()
  private readonly inflight = new Map<string, Promise<Uint8Array>>()
  private cacheBytes = 0
  private generation = 0

  constructor(options: ContentRangeCacheOptions = {}) {
    // Browser fetch requires its Window receiver; injected fetchers keep their own binding.
    this.fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis)
    this.baseUrl = options.baseUrl ?? '/api/content/read'
    this.chunkBytes = Math.max(1, Math.min(options.chunkBytes ?? DEFAULT_CHUNK_BYTES, 1024 * 1024))
    this.maxBytes = Math.max(1, Math.min(options.maxBytes ?? DEFAULT_MAX_BYTES, 64 * 1024 * 1024))
    this.maxEntries = Math.max(1, Math.min(options.maxEntries ?? DEFAULT_MAX_ENTRIES, 256))
  }

  async read(
    ref: ContentRangeRef,
    options: { offset?: number; limit?: number; signal?: AbortSignal } = {},
  ): Promise<Uint8Array> {
    const offset = options.offset ?? 0
    const limit = Math.min(options.limit ?? this.chunkBytes, this.chunkBytes)
    if (!Number.isSafeInteger(offset) || offset < 0) throw new RangeError('offset must be non-negative')
    if (!Number.isSafeInteger(limit) || limit <= 0) throw new RangeError('limit must be positive')
    if (options.signal?.aborted) throw abortError()
    const key = keyFor(ref, offset, limit)
    const cached = this.cache.get(key)
    if (cached) return cached.slice()
    const existing = this.inflight.get(key)
    if (existing) return (await existing).slice()
    // The request is shared by all waiters.  Passing one waiter's signal into
    // the shared fetch lets an unmount/session switch abort a different
    // consumer's request.  Keep the bounded (<=1 MiB) transport alive and
    // fence the individual waiter before returning; a subsequent waiter can
    // still reuse the in-flight result safely.
    const pending = this.fetchRange(ref, offset, limit)
    const generation = this.generation
    this.inflight.set(key, pending)
    try {
      const value = await pending
      if (options.signal?.aborted) throw abortError()
      // A session switch may clear the cache while this request is still in
      // flight. Do not let the old response repopulate the new session's
      // cache; the caller's own epoch fence handles the returned value.
      if (generation !== this.generation) return value.slice()
      // Keep both dimensions bounded. `maxEntries` alone is insufficient when
      // callers choose a large chunk size (up to 1 MiB): a full cache could
      // otherwise exceed the advertised `maxBytes` cap by many times.
      const previous = this.cache.get(key)
      if (previous) this.cacheBytes -= previous.byteLength
      this.cache.set(key, value)
      this.cacheBytes += value.byteLength
      while (this.cache.size > this.maxEntries || this.cacheBytes > this.maxBytes) {
        const oldest = this.cache.keys().next().value
        if (oldest === undefined) break
        const evicted = this.cache.get(oldest)
        this.cache.delete(oldest)
        if (evicted) this.cacheBytes -= evicted.byteLength
      }
      return value.slice()
    } finally {
      if (this.inflight.get(key) === pending) this.inflight.delete(key)
    }
  }

  canReadText(ref: ContentRangeRef): boolean {
    const total = Number(ref.byteLength ?? 0)
    return Number.isSafeInteger(total) && total > 0 && total <= this.maxBytes
  }

  /** Read a known-size raw row with bounded chunks and strict forward progress. */
  async readText(
    ref: ContentRangeRef,
    options: { signal?: AbortSignal } = {},
  ): Promise<string> {
    const total = Number(ref.byteLength ?? 0)
    if (!this.canReadText(ref)) {
      throw new Error('content.read.v1 content is too large or has no length')
    }
    const decoder = new TextDecoder('utf-8', { fatal: true })
    const parts: string[] = []
    let offset = 0
    while (offset < total) {
      const requested = Math.min(this.chunkBytes, total - offset)
      const bytes = await this.read(ref, { offset, limit: requested, signal: options.signal })
      if (bytes.byteLength === 0) throw new Error('content.read.v1 returned an empty range')
      if (bytes.byteLength > requested || offset + bytes.byteLength > total) {
        throw new Error('content.read.v1 returned an invalid range')
      }
      offset += bytes.byteLength
      parts.push(decoder.decode(bytes, { stream: offset < total }))
    }
    parts.push(decoder.decode())
    return parts.join('')
  }

  async readDisplay(
    ref: ContentRangeRef,
    options: { signal?: AbortSignal } = {},
  ): Promise<string> {
    if (ref.view !== 'display') throw new RangeError('display view is required')
    const url = `${this.baseUrl}?sessionKey=${encode(ref.sessionKey)}&sessionId=${encode(ref.sessionId)}&messageId=${encode(ref.messageId)}${ref.source ? `&source=${encode(ref.source)}` : ''}${ref.revision ? `&revision=${encode(ref.revision)}` : ''}&view=display&export=1`
    const response = await this.fetcher(url, {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      signal: options.signal,
    })
    if (!response.ok) throw new Error(`content.read.v1 display failed (${response.status})`)
    // `Response.text()` allocates the complete transformed row before the
    // cache can enforce its bound.  Display rows may be produced from legacy
    // tool/assistant payloads, so consume the body incrementally and fail
    // closed once the renderer's byte budget is exceeded.
    const declaredLength = Number(response.headers.get('content-length') ?? '')
    if (Number.isFinite(declaredLength) && declaredLength > this.maxBytes) {
      try { await response.body?.cancel() } catch { /* best effort */ }
      throw new Error('content.read.v1 display is too large')
    }
    const reader = response.body?.getReader()
    if (!reader) {
      if (!Number.isFinite(declaredLength)) {
        throw new Error('content.read.v1 display body is not streamable')
      }
      const text = await response.text()
      if (new TextEncoder().encode(text).byteLength > this.maxBytes) {
        throw new Error('content.read.v1 display is too large')
      }
      return text
    }
    const decoder = new TextDecoder('utf-8', { fatal: true })
    const parts: string[] = []
    let received = 0
    let streamCompleted = false
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          streamCompleted = true
          break
        }
        const bytes = value instanceof Uint8Array ? value : new Uint8Array(value)
        received += bytes.byteLength
        if (received > this.maxBytes) throw new Error('content.read.v1 display is too large')
        parts.push(decoder.decode(bytes, { stream: true }))
      }
      parts.push(decoder.decode())
      return parts.join('')
    } finally {
      // An oversized or failed body may still have unread bytes in the
      // network stream.  Release the reader on every non-terminal path so a
      // rejected hydration cannot keep the renderer's response alive.
      if (!streamCompleted || options.signal?.aborted) {
        try { await reader.cancel() } catch { /* best effort */ }
      }
      // Release the body lock after success as well as cancellation.
      try { reader.releaseLock() } catch { /* best effort */ }
    }
  }

  clear(): void {
    this.generation += 1
    this.cache.clear()
    this.inflight.clear()
    this.cacheBytes = 0
  }

  private async fetchRange(
    ref: ContentRangeRef,
    offset: number,
    limit: number,
  ): Promise<Uint8Array> {
    const url = `${this.baseUrl}?sessionKey=${encode(ref.sessionKey)}&sessionId=${encode(ref.sessionId)}&messageId=${encode(ref.messageId)}${ref.source ? `&source=${encode(ref.source)}` : ''}${ref.revision ? `&revision=${encode(ref.revision)}` : ''}&offset=${offset}&limit=${limit}`
    const response = await this.fetcher(url, {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Range: `bytes=${offset}-${offset + limit - 1}` },
    })
    if (!response.ok) throw new Error(`content.read.v1 failed (${response.status})`)
    const body = response.body
    if (!body) {
      const bytes = new Uint8Array(await response.arrayBuffer())
      if (bytes.byteLength > limit || bytes.byteLength > this.maxBytes) {
        throw new Error('content.read.v1 returned an oversized range')
      }
      return bytes
    }
    // Consume the bounded response through a reader instead of
    // Response.arrayBuffer().  The latter creates an opaque body-sized
    // allocation before this layer can apply its cap.  A normal range is
    // already limited to <=1 MiB, but retaining the explicit bound here keeps
    // malformed servers from turning one request into an unbounded renderer
    // allocation and lets us release the stream lock on every path.
    const reader = body.getReader()
    const chunks: Uint8Array[] = []
    let received = 0
    let completed = false
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          completed = true
          break
        }
        if (!value) continue
        received += value.byteLength
        if (received > limit || received > this.maxBytes) {
          throw new Error('content.read.v1 returned an oversized range')
        }
        chunks.push(value)
      }
      if (chunks.length === 0) return new Uint8Array()
      if (chunks.length === 1) return chunks[0]!
      const bytes = new Uint8Array(received)
      let cursor = 0
      for (const chunk of chunks) {
        bytes.set(chunk, cursor)
        cursor += chunk.byteLength
      }
      return bytes
    } finally {
      if (!completed) {
        try { await reader.cancel() } catch { /* best effort */ }
      }
      try { reader.releaseLock() } catch { /* best effort */ }
    }
  }
}
