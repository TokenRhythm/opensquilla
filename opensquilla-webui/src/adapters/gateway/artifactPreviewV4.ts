import { onUnmounted, ref, shallowRef } from 'vue'

/**
 * Shared preview loader for artifact thumbnails and full images.
 *
 * Goals (page performance + slow public-network resilience):
 *  - Viewport lazy-load: a card fetches its preview only once it scrolls into
 *    view (IntersectionObserver). Off-screen cards never pre-fetch.
 *  - Concurrency cap: a small global queue keeps at most a few preview fetches
 *    in flight so a multi-image turn cannot saturate the link.
 *  - Real progress on slow links: fetch + ReadableStream + Content-Length gives
 *    a percentage when the server reports a length, otherwise an indeterminate
 *    state drives a shimmer.
 *  - Bounded timeout + retry: a stalled fetch flips to `timeout`/`error` with a
 *    Retry affordance instead of a permanent "loading" dead-end.
 *  - Memory: callers can reject oversized responses before buffering them;
 *    full-size blob URLs are tracked in a bounded LRU and the oldest is revoked
 *    past the cap. Thumbnails are tiny and kept with the card.
 */

export type ArtifactPreviewState = 'idle' | 'loading' | 'loaded' | 'timeout' | 'error'
export type ArtifactPreviewErrorCode = 'network' | 'too_large' | 'unsupported' | null

export interface ArtifactPreviewOptions {
  /** Loader resolving the URL to fetch (thumbnail for grids, full for lightbox). */
  resolveUrl: () => string
  /** Request headers (auth) for same-origin fetches. */
  headers?: () => Record<string, string>
  /** Whether the resolved URL is same-origin (drives credentials mode). */
  sameOrigin?: (url: string) => boolean
  /** Full-size previews participate in the bounded LRU; thumbnails do not. */
  fullSize?: boolean
  /** Per-attempt timeout in ms. */
  timeoutMs?: number
  /** Max automatic-eligible retries (manual retry always allowed afterwards). */
  maxRetries?: number
  /** Maximum response bytes retained for a preview. Omit for no size cap. */
  maxBytes?: number
  /** Reject cross-origin URLs before any unauthenticated preview request. */
  requireSameOrigin?: boolean
  /** Validate response bytes before allocating an object URL. */
  acceptBlob?: (blob: Blob) => boolean
}

const CONCURRENCY = 3
const FULL_LRU_LIMIT = 8

// ── Global concurrency-capped queue ──────────────────────────────────────────
// Keeps at most CONCURRENCY preview fetches running; the rest wait their turn.
let activeCount = 0
const waiters: Array<() => void> = []

function acquireSlot(): Promise<void> {
  if (activeCount < CONCURRENCY) {
    activeCount += 1
    return Promise.resolve()
  }
  return new Promise<void>(resolve => {
    waiters.push(() => {
      activeCount += 1
      resolve()
    })
  })
}

function releaseSlot() {
  activeCount = Math.max(0, activeCount - 1)
  const next = waiters.shift()
  if (next) next()
}

// ── Bounded LRU of full-size blob URLs ───────────────────────────────────────
// Thumbnails are tiny and excluded; only full-size images can pile up, so the
// oldest object URL is revoked once the cap is exceeded.
const fullLru = new Map<string, string>()

function trackFullUrl(token: string, objectUrl: string) {
  if (fullLru.has(token)) {
    try { URL.revokeObjectURL(fullLru.get(token) as string) } catch {}
    fullLru.delete(token)
  }
  fullLru.set(token, objectUrl)
  while (fullLru.size > FULL_LRU_LIMIT) {
    const oldestKey = fullLru.keys().next().value as string | undefined
    if (oldestKey === undefined) break
    const oldestUrl = fullLru.get(oldestKey)
    fullLru.delete(oldestKey)
    if (oldestUrl) {
      try { URL.revokeObjectURL(oldestUrl) } catch {}
    }
  }
}

function untrackFullUrl(token: string) {
  if (!fullLru.has(token)) return
  try { URL.revokeObjectURL(fullLru.get(token) as string) } catch {}
  fullLru.delete(token)
}

function defaultSameOrigin(url: string): boolean {
  try {
    return new URL(url, window.location.origin).origin === window.location.origin
  } catch { return false }
}

let tokenSeq = 0

export interface ArtifactPreviewController {
  state: ReturnType<typeof ref<ArtifactPreviewState>>
  errorCode: ReturnType<typeof ref<ArtifactPreviewErrorCode>>
  progress: ReturnType<typeof ref<number | null>>
  objectUrl: ReturnType<typeof shallowRef<string>>
  load: () => void
  retry: () => void
  observe: (el: Element | null) => void
  release: () => void
  /** Permanently dispose the controller (release + ignore further calls). */
  dispose: () => void
}

/**
 * Lifecycle-free preview controller. A list component that owns many cards can
 * create one of these per card and dispose them itself; `useArtifactPreview`
 * wraps it with `onUnmounted` for single-instance call sites.
 */
export function createArtifactPreview(options: ArtifactPreviewOptions): ArtifactPreviewController {
  const state = ref<ArtifactPreviewState>('idle')
  const errorCode = ref<ArtifactPreviewErrorCode>(null)
  // null progress = indeterminate (no Content-Length); 0–100 otherwise.
  const progress = ref<number | null>(null)
  const objectUrl = shallowRef<string>('')

  const timeoutMs = options.timeoutMs ?? 30000
  const maxRetries = options.maxRetries ?? 2
  const maxBytes = typeof options.maxBytes === 'number'
    && Number.isFinite(options.maxBytes)
    && options.maxBytes > 0
    ? options.maxBytes
    : Number.POSITIVE_INFINITY
  const fullSize = options.fullSize === true
  const sameOriginFn = options.sameOrigin ?? defaultSameOrigin
  const lruToken = `preview-${(tokenSeq += 1)}`

  let attempt = 0
  let runSeq = 0
  let observer: IntersectionObserver | null = null
  let observedEl: Element | null = null
  let inFlight = false
  let disposed = false
  let activeAbortController: AbortController | null = null
  let retryTimer: number | null = null

  function setObjectUrl(next: string) {
    const prev = objectUrl.value
    objectUrl.value = next
    if (fullSize) {
      if (next) trackFullUrl(lruToken, next)
    } else if (prev && prev !== next) {
      try { URL.revokeObjectURL(prev) } catch {}
    }
  }

  async function run() {
    if (disposed || inFlight) return
    if (state.value === 'loaded' && objectUrl.value) return
    const url = options.resolveUrl()
    if (!url) {
      state.value = 'error'
      errorCode.value = 'network'
      return
    }
    inFlight = true
    const seq = ++runSeq
    state.value = 'loading'
    errorCode.value = null
    progress.value = null

    await acquireSlot()
    if (disposed || seq !== runSeq) {
      releaseSlot()
      inFlight = false
      return
    }

    const controller = new AbortController()
    activeAbortController = controller
    const timer = window.setTimeout(() => controller.abort('timeout'), timeoutMs)
    let timedOut = false

    try {
      const isSame = sameOriginFn(url)
      if (options.requireSameOrigin && !isSame) {
        throw new ArtifactPreviewLoadError('network', 'Cross-origin preview is not allowed')
      }
      const response = await fetch(url, {
        method: 'GET',
        headers: isSame && options.headers ? options.headers() : {},
        credentials: isSame ? 'same-origin' : 'omit',
        signal: controller.signal,
        redirect: 'error',
      })
      if (!response.ok) throw new Error(`status ${response.status}`)

      const blob = await readBlobWithProgress(response, p => {
        if (seq === runSeq) progress.value = p
      }, maxBytes)
      if (disposed || seq !== runSeq) return
      if (options.acceptBlob && !options.acceptBlob(blob)) {
        throw new ArtifactPreviewLoadError('unsupported', 'Preview media type is unsupported')
      }

      const nextUrl = URL.createObjectURL(blob)
      setObjectUrl(nextUrl)
      progress.value = 100
      state.value = 'loaded'
    } catch (err) {
      if (disposed || seq !== runSeq) return
      timedOut = controller.signal.aborted &&
        (controller.signal.reason === 'timeout' || (err as DOMException)?.name === 'AbortError')
      errorCode.value = err instanceof ArtifactPreviewLoadError ? err.code : 'network'
      state.value = timedOut ? 'timeout' : 'error'
    } finally {
      window.clearTimeout(timer)
      if (activeAbortController === controller) activeAbortController = null
      releaseSlot()
      inFlight = false
    }
  }

  function load() {
    if (state.value === 'loaded' || inFlight) return
    void run()
  }

  function retry() {
    if (inFlight) return
    if (retryTimer !== null) window.clearTimeout(retryTimer)
    attempt += 1
    const eligible = attempt <= maxRetries
    // Gentle backoff for auto-eligible retries; manual retries beyond the cap
    // fire immediately so the user is never blocked.
    const delay = eligible ? Math.min(2000, 300 * attempt) : 0
    state.value = 'loading'
    errorCode.value = null
    progress.value = null
    const retrySeq = runSeq
    retryTimer = window.setTimeout(() => {
      retryTimer = null
      if (!disposed && retrySeq === runSeq) void run()
    }, delay)
  }

  function observe(el: Element | null) {
    cancelObserve()
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      // Without IO support, load eagerly so the preview never stays blank.
      load()
      return
    }
    observedEl = el
    observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          cancelObserve()
          load()
          break
        }
      }
    }, { rootMargin: '200px' })
    observer.observe(el)
  }

  function cancelObserve() {
    if (observer && observedEl) observer.unobserve(observedEl)
    observer?.disconnect()
    observer = null
    observedEl = null
  }

  function release() {
    cancelObserve()
    runSeq += 1
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer)
      retryTimer = null
    }
    activeAbortController?.abort('cancelled')
    activeAbortController = null
    if (fullSize) {
      untrackFullUrl(lruToken)
    } else if (objectUrl.value) {
      try { URL.revokeObjectURL(objectUrl.value) } catch {}
    }
    objectUrl.value = ''
    state.value = 'idle'
    errorCode.value = null
    progress.value = null
    attempt = 0
  }

  function dispose() {
    disposed = true
    release()
  }

  return { state, errorCode, progress, objectUrl, load, retry, observe, release, dispose }
}

/**
 * Single-instance wrapper: ties controller disposal to the component lifecycle.
 */
export function useArtifactPreview(options: ArtifactPreviewOptions): ArtifactPreviewController {
  const controller = createArtifactPreview(options)
  onUnmounted(() => controller.dispose())
  return controller
}

/**
 * Stream the response body so a Content-Length header yields a real percentage
 * for slow transfers; without it, progress stays indeterminate (null).
 */
class ArtifactPreviewLoadError extends Error {
  constructor(readonly code: Exclude<ArtifactPreviewErrorCode, null>, message: string) {
    super(message)
    this.name = 'ArtifactPreviewLoadError'
  }
}

async function readBlobWithProgress(
  response: Response,
  onProgress: (percent: number | null) => void,
  maxBytes = Number.POSITIVE_INFINITY,
): Promise<Blob> {
  const lengthHeader = response.headers.get('Content-Length')
  const total = lengthHeader ? Number(lengthHeader) : 0
  const body = response.body
  const type = response.headers.get('Content-Type') || ''
  const hasKnownTotal = Number.isFinite(total) && total > 0

  if (hasKnownTotal && total > maxBytes) {
    try { await body?.cancel() } catch {}
    throw new ArtifactPreviewLoadError('too_large', 'Preview response is too large')
  }

  if (!body) {
    onProgress(null)
    const blob = await response.blob()
    if (blob.size > maxBytes) {
      throw new ArtifactPreviewLoadError('too_large', 'Preview response is too large')
    }
    return blob
  }

  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  onProgress(hasKnownTotal ? 0 : null)
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    if (value) {
      chunks.push(value)
      received += value.length
      if (received > maxBytes) {
        try { await reader.cancel() } catch {}
        throw new ArtifactPreviewLoadError('too_large', 'Preview response is too large')
      }
      if (hasKnownTotal) {
        onProgress(Math.min(99, Math.round((received / total) * 100)))
      }
    }
  }
  return new Blob(chunks as BlobPart[], type ? { type } : undefined)
}
