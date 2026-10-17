import { computed, inject, onScopeDispose, readonly, ref, watch } from 'vue'
import { boundedTextPreview } from '@/utils/chat/toolContentPreview'
import { saveByteStream } from '@/utils/streamDownload'
import { readInlineToolContent, reserveInlineToolPreview, type InlineToolPreviewReservation } from './inlineToolContentBudget'
import {
  SESSION_INSPECTION_KEY,
  SessionInspectionContractError,
  SessionInspectionLogNotReadyError,
} from '@/modules/sessionInspection'

export interface InlineExecutionLogOptions {
  sessionKey: () => string | undefined
  handle: () => string | undefined
  toolId: () => string | undefined
  enabled?: () => boolean
}

/** Owned by the expanded tool detail. Closing that detail aborts its reads. */
export function useInlineExecutionLog(options: InlineExecutionLogOptions) {
  const inspection = inject(SESSION_INSPECTION_KEY, null)
  const content = ref<string | null>(null)
  const loading = ref(false)
  // `complete` means every saved page was read, not that the producer retained
  // every byte of its output. `partial` preserves that separate server fact.
  const complete = ref(false)
  const hasMore = ref(false)
  const partial = ref(false)
  const truncated = ref(false)
  const error = ref<'not-ready' | 'failed' | null>(null)
  const source = computed(() => ({
    sessionKey: options.sessionKey()?.trim() || '',
    handle: options.handle()?.trim() || '',
    toolId: options.toolId()?.trim() || '',
    enabled: options.enabled?.() !== false,
  }))
  const key = computed(() => JSON.stringify(source.value))
  const available = computed(() => Boolean(inspection && source.value.enabled
    && source.value.sessionKey && source.value.handle && source.value.toolId))
  interface LogRead {
    controller: AbortController
    key: string
    generation: number
    sessionKey: string
    handle: string
    nextOffset: number | null
    totalChars?: number
    content: string
    pending: Promise<boolean> | null
    reservation: InlineToolPreviewReservation
  }
  let request: LogRead | null = null
  let generation = 0
  let disposed = false

  function clear() {
    content.value = null
    loading.value = false
    complete.value = false
    hasMore.value = false
    partial.value = false
    truncated.value = false
    error.value = null
  }

  function cancel() {
    generation += 1
    request?.controller.abort()
    request?.reservation.release()
    if (request) request.content = ''
    request = null
  }

  function current(run: LogRead): boolean {
    return !disposed && !run.controller.signal.aborted && request === run
      && generation === run.generation && key.value === run.key
  }

  async function readNext(run: LogRead): Promise<boolean> {
    if (!inspection || !current(run) || run.nextOffset === null) return false
    const offset = run.nextOffset
    const { sessionKey, handle, controller } = run
    loading.value = true
    try {
      const capacity = run.reservation.prepare(controller.signal)
      if (capacity) await capacity
      if (!current(run)) return false
      const page = await readInlineToolContent(controller.signal, () => {
        if (!current(run)) throw new DOMException('Aborted', 'AbortError')
        return inspection.readExecutionLog(sessionKey, handle, offset, { signal: controller.signal })
      })
      if (!page || !current(run)) return false
      const returnedChars = Array.from(page.content).length
      const end = offset + returnedChars
      // Refuse gaps, overlapping pages and a changing snapshot rather than
      // silently joining unrelated data or looping on a non-advancing cursor.
      if (page.handle !== handle || page.offset !== offset
        || !Number.isSafeInteger(page.chars) || page.chars < 0
        || (run.totalChars !== undefined && page.chars !== run.totalChars)
        || end > page.chars
        || (end < page.chars && (returnedChars === 0 || page.nextOffset !== end))
        || (end === page.chars && page.nextOffset !== null)) {
        throw new SessionInspectionContractError('Inconsistent inline execution log page.')
      }
      run.totalChars = page.chars
      run.nextOffset = page.nextOffset
      const preview = boundedTextPreview(run.content + page.content)
      run.content = preview.content
      truncated.value = preview.truncated
      content.value = run.content
      partial.value ||= page.complete !== true
      hasMore.value = page.nextOffset !== null && !truncated.value
      complete.value = page.nextOffset === null && !truncated.value
      return true
    } catch (cause) {
      if (current(run)) {
        error.value = cause instanceof SessionInspectionLogNotReadyError ? 'not-ready' : 'failed'
      }
      return false
    } finally {
      if (current(run)) {
        // Real failures retain only the displayed prefix; capacity waits never
        // become read errors and resume as other details release their text.
        run.reservation.retain(run.content)
        loading.value = false
      }
    }
  }

  /** One page per explicit scroll demand; concurrent demands share the read. */
  function loadMore(): Promise<boolean> {
    const run = request
    if (!run || !current(run)) return Promise.resolve(false)
    if (run.pending) return run.pending
    if (truncated.value) return Promise.resolve(false)
    if (complete.value) return Promise.resolve(true)
    if (error.value) return Promise.resolve(false)
    const pending = readNext(run)
    run.pending = pending
    void pending.then(() => { if (run.pending === pending) run.pending = null })
    return pending
  }

  /** Only call for a user action that needs all saved text, such as copy. */
  async function ensureComplete(): Promise<boolean> {
    const run = request
    if (!run || !current(run)) return false
    while (current(run) && !complete.value) {
      if (!await loadMore()) return false
    }
    return current(run) && complete.value
  }

  /** Separate bounded pages go directly to disk; they never join the card's state. */
  async function download(): Promise<'saved' | 'cancelled'> {
    const run = request
    if (!inspection || !run || !current(run)) throw new Error('Execution log unavailable')
    const service = inspection
    let offset = 0
    let total: number | undefined
    const downloadController = new AbortController()
    const cancelDownload = () => downloadController.abort()
    run.controller.signal.addEventListener('abort', cancelDownload, { once: true })
    try {
      return await saveByteStream('command-output.txt', async () => new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            if (!current(run)) throw new DOMException('Aborted', 'AbortError')
            const page = await readInlineToolContent(downloadController.signal, () => {
              if (!current(run)) throw new DOMException('Aborted', 'AbortError')
              return service.readExecutionLog(run.sessionKey, run.handle, offset, { signal: downloadController.signal })
            })
            if (!current(run)) throw new DOMException('Aborted', 'AbortError')
            const end = offset + Array.from(page.content).length
            if (page.handle !== run.handle || page.offset !== offset
              || !Number.isSafeInteger(page.chars) || page.chars < 0 || end > page.chars
              || (total !== undefined && page.chars !== total)
              || (end < page.chars && (end === offset || page.nextOffset !== end))
              || (end === page.chars && page.nextOffset !== null)) throw new Error('Inconsistent execution log')
            total = page.chars
            offset = end
            controller.enqueue(new TextEncoder().encode(page.content))
            if (page.nextOffset === null) controller.close()
          } catch (cause) { controller.error(cause) }
        },
        cancel: cancelDownload,
      }), { signal: downloadController.signal, mime: 'text/plain;charset=utf-8' })
    } finally {
      run.controller.signal.removeEventListener('abort', cancelDownload)
      cancelDownload()
    }
  }

  async function retry(): Promise<void> {
    cancel()
    clear()
    if (disposed || !inspection || !available.value) return
    request = { controller: new AbortController(), key: key.value, generation,
      sessionKey: source.value.sessionKey, handle: source.value.handle,
      nextOffset: 0, content: '', pending: null, reservation: reserveInlineToolPreview() }
    await loadMore()
  }

  watch(key, () => { void retry() }, { immediate: true })
  onScopeDispose(() => { disposed = true; cancel(); clear() })

  return { available, content: readonly(content), loading: readonly(loading), complete: readonly(complete),
    hasMore: readonly(hasMore), partial: readonly(partial), truncated: readonly(truncated),
    error: readonly(error), retry, loadMore, ensureComplete, download }
}
