export interface OfficePageSize { width: number; height: number }
export interface OfficePageThumbnail extends OfficePageSize { blob: Blob }

const MAX_PENDING = 24
const MAX_CACHED = 24
const MAX_CACHE_BYTES = 4 * 1024 * 1024

/** One temporary canvas, one render at a time, and bounded encoded-image cache.
 * The caller owns any Object URLs it creates from returned Blobs.
 */
export function createOfficeThumbnails(options: {
  document: Document
  getPageSize(index: number): OfficePageSize | null
  render(canvas: HTMLCanvasElement, index: number, width: number): Promise<void>
}) {
  type Job = { index: number; signal?: AbortSignal; resolve(value: OfficePageThumbnail): void;
    reject(error: unknown): void; abort(): void; settled: boolean }
  const queue: Job[] = []
  const cache = new Map<number, OfficePageThumbnail>()
  let cacheBytes = 0
  let destroyed = false
  let scratch: HTMLCanvasElement | null = null
  let active: Job | null = null
  let activeDone: Promise<void> | null = null
  const aborted = () => new DOMException('Thumbnail request cancelled', 'AbortError')
  function finish(job: Job, value?: OfficePageThumbnail, error?: unknown) {
    if (job.settled) return
    job.settled = true
    job.signal?.removeEventListener('abort', job.abort)
    if (value) job.resolve(value)
    else job.reject(error || aborted())
  }
  function cached(index: number) {
    const value = cache.get(index)
    if (value) { cache.delete(index); cache.set(index, value) }
    return value
  }
  function remember(index: number, value: OfficePageThumbnail) {
    if (value.blob.size > MAX_CACHE_BYTES) return
    cacheBytes -= cache.get(index)?.blob.size || 0
    cache.delete(index); cache.set(index, value); cacheBytes += value.blob.size
    while (cache.size > MAX_CACHED || cacheBytes > MAX_CACHE_BYTES) {
      const oldest = cache.keys().next().value
      if (oldest === undefined) break
      cacheBytes -= cache.get(oldest)!.blob.size; cache.delete(oldest)
    }
  }
  async function run(job: Job) {
    try {
      const previous = cached(job.index)
      if (previous) { finish(job, previous); return }
      const size = options.getPageSize(job.index)
      if (!size || !Number.isFinite(size.width) || !Number.isFinite(size.height) || size.width <= 0 || size.height <= 0) {
        throw new RangeError('Page is unavailable')
      }
      const width = Math.max(1, Math.floor(Math.min(160, 240 * size.width / size.height)))
      if (width * size.height / size.width > 241) throw new RangeError('Page proportions exceed the thumbnail budget')
      scratch ||= options.document.createElement('canvas')
      await options.render(scratch, job.index, width)
      if (destroyed || job.settled || job.signal?.aborted) return
      const canvas = scratch
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => {
        if (value) resolve(value)
        else reject(new Error('Thumbnail encoding failed'))
      }, 'image/png'))
      if (destroyed || job.settled || job.signal?.aborted) return
      const value = { blob, width: canvas.width, height: canvas.height }
      remember(job.index, value)
      finish(job, value)
    } catch (error) { finish(job, undefined, error) }
    finally {
      // Release pixels even when cancellation arrived inside the renderer.
      if (scratch) { scratch.width = 0; scratch.height = 0 }
    }
  }
  function pump() {
    if (destroyed || active) return
    const job = queue.shift()
    if (!job) return
    active = job
    activeDone = run(job).finally(() => {
      active = null; activeDone = null
      pump()
    })
  }
  return {
    request(index: number, signal?: AbortSignal): Promise<OfficePageThumbnail> {
      if (destroyed || signal?.aborted) return Promise.reject(aborted())
      if (!Number.isInteger(index) || index < 0 || !options.getPageSize(index)) return Promise.reject(new RangeError('Page is unavailable'))
      const previous = cached(index)
      if (previous) return Promise.resolve(previous)
      if (queue.length >= MAX_PENDING) return Promise.reject(aborted())
      return new Promise((resolve, reject) => {
        const job: Job = { index, signal, resolve, reject, settled: false, abort: () => {
          const position = queue.indexOf(job)
          if (position >= 0) queue.splice(position, 1)
          finish(job)
        } }
        signal?.addEventListener('abort', job.abort, { once: true })
        queue.push(job); pump()
      })
    },
    /** Borrowed models must remain alive until an already-running render settles. */
    destroy(): Promise<void> | null {
      destroyed = true
      for (const job of queue.splice(0)) finish(job)
      if (active) finish(active)
      cache.clear(); cacheBytes = 0
      if (!active && scratch) { scratch.width = 0; scratch.height = 0; scratch = null }
      return activeDone
    },
  }
}
