import { TOOL_CONTENT_PREVIEW_MAX_BYTES } from '@/utils/chat/toolContentPreview'

// Reserve a bounded response while it is read, then charge only retained UTF-16
// text. Many small expanded cards must not exhaust sixteen maximum-size slots.
const ACTIVE_PREVIEW_BUDGET = 8 * 1024 * 1024
const PREVIEW_RESERVATION = TOOL_CONTENT_PREVIEW_MAX_BYTES * 2
const MAX_ACTIVE_READS = 2
let reservedBytes = 0
let activeReads = 0
const waiting: Array<() => void> = []
const capacityWaiting: Array<() => boolean> = []
let resumingCapacity = false

function resumeCapacityReads() {
  if (resumingCapacity) return
  resumingCapacity = true
  try {
    while (capacityWaiting.length) {
      const next = capacityWaiting[0]!
      if (!next()) break
      const index = capacityWaiting.indexOf(next)
      if (index >= 0) capacityWaiting.splice(index, 1)
    }
  } finally { resumingCapacity = false }
}

export interface InlineToolPreviewReservation {
  prepare: (signal: AbortSignal) => void | Promise<void>
  retain: (content: string) => void
  release: () => void
}

export function reserveInlineToolPreview(): InlineToolPreviewReservation {
  let bytes = 0
  let released = false
  let cancelPending: (() => void) | null = null
  const release = () => {
    if (released) return
    released = true
    cancelPending?.()
    reservedBytes -= bytes
    bytes = 0
    resumeCapacityReads()
  }
  return {
    prepare(signal) {
      if (signal.aborted || released) return Promise.reject(new DOMException('Aborted', 'AbortError'))
      const additional = PREVIEW_RESERVATION - bytes
      if (reservedBytes + additional <= ACTIVE_PREVIEW_BUDGET) {
        reservedBytes += additional
        bytes = PREVIEW_RESERVATION
        return
      }
      return new Promise((resolve, reject) => {
        let settled = false
        const cleanup = () => {
          signal.removeEventListener('abort', abort)
          cancelPending = null
        }
        const abort = () => {
          if (settled) return
          settled = true
          cleanup()
          const index = capacityWaiting.indexOf(start)
          if (index >= 0) capacityWaiting.splice(index, 1)
          reject(new DOMException('Aborted', 'AbortError'))
          resumeCapacityReads()
        }
        const start = () => {
          if (settled) return true
          if (signal.aborted || released) { abort(); return true }
          const additional = PREVIEW_RESERVATION - bytes
          if (reservedBytes + additional > ACTIVE_PREVIEW_BUDGET) return false
          reservedBytes += additional
          bytes = PREVIEW_RESERVATION
          settled = true
          cleanup()
          resolve()
          return true
        }
        cancelPending = abort
        signal.addEventListener('abort', abort, { once: true })
        if (!start()) capacityWaiting.push(start)
      })
    },
    retain(content) {
      if (released) return
      const retainedBytes = Math.min(PREVIEW_RESERVATION, content.length * 2)
      reservedBytes -= bytes - retainedBytes
      bytes = retainedBytes
      resumeCapacityReads()
    },
    release,
  }
}

/** One shared read limit for preview pages and streaming download pages. */
export function readInlineToolContent<T>(signal: AbortSignal, read: () => Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    let started = false
    let settled = false
    const finish = () => {
      if (settled) return false
      settled = true
      signal.removeEventListener('abort', abort)
      if (started) {
        activeReads -= 1
        waiting.shift()?.()
      } else {
        const index = waiting.indexOf(start)
        if (index >= 0) waiting.splice(index, 1)
      }
      return true
    }
    const abort = () => {
      if (finish()) reject(new DOMException('Aborted', 'AbortError'))
    }
    const start = () => {
      if (signal.aborted) { abort(); return }
      started = true
      activeReads += 1
      // Invoke immediately when capacity is free. Also contain synchronous
      // transport failures and ignore late replies after cancellation.
      try {
        read().then(value => { if (finish()) resolve(value) }, cause => { if (finish()) reject(cause) })
      } catch (cause) {
        if (finish()) reject(cause)
      }
    }
    if (signal.aborted) { abort(); return }
    signal.addEventListener('abort', abort, { once: true })
    if (activeReads < MAX_ACTIVE_READS) start()
    else waiting.push(start)
  })
}
