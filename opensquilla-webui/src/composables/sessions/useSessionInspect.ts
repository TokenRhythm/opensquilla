import { ref } from 'vue'
import type { SessionInspection } from '@/modules/sessionInspection'
import type { SessionReadMessage } from '@/modules/sessionReadLifecycle'
import type { TurnCommands } from '@/modules/turnCommands'

// The inspect drawer composes a bounded preview with canonical transcript pages.

export const SESSION_INSPECT_PAGE_SIZE = 20

export async function abortInspectedSession(
  turnCommands: Pick<TurnCommands, 'cancel'>,
  sessionKey: string,
): Promise<boolean> {
  const result = await turnCommands.cancel({
    sessionKey,
    source: 'session-inspection',
  })
  return result.aborted === true
}

function transcriptMessageKey(msg: SessionReadMessage): string {
  return String(
    msg.messageId
    || msg.id
    || `${msg.role}:${msg.createdAt ?? ''}:${msg.text}`,
  )
}

export function useSessionInspect(sessionInspection: SessionInspection) {
  const preview = ref<Awaited<ReturnType<SessionInspection['preview']>>>(null)
  const messages = ref<SessionReadMessage[]>([])
  const hasEarlier = ref(false)
  const loading = ref(false)
  const loadingEarlier = ref(false)
  const loadEarlierError = ref(false)
  const transcriptError = ref(false)
  const canonicalAvailable = ref<boolean | null>(null)
  const canonicalComplete = ref<boolean | null>(null)
  const oldestCursor = ref<string | number | null>(null)

  let requestSeq = 0
  let currentKey = ''
  let activeController: AbortController | null = null
  let failedTranscriptRequest: {
    key: string
    before: string | number | null
  } | null = null
  const loadedEarlierCursors = new Set<string>()

  async function fetchPreview(key: string, seq: number, signal: AbortSignal) {
    try {
      const row = await sessionInspection.preview(key, { signal })
      if (seq !== requestSeq) return
      preview.value = row
    } catch {
      // Preview is a summary garnish; header data falls back to the ledger
      // row and transcript failures are surfaced separately.
      if (seq === requestSeq) preview.value = null
    }
  }

  async function fetchTranscript(
    key: string,
    seq: number,
    signal: AbortSignal,
    before?: string | number | null,
    beforeApply?: () => void,
  ) {
    const historyOptions = {
      limit: SESSION_INSPECT_PAGE_SIZE,
      signal,
    }
    const data = before == null
      ? await sessionInspection.history.latest(key, historyOptions)
      : await sessionInspection.history.before(key, String(before), historyOptions)
    if (seq !== requestSeq) return
    const available = data.canonicalAvailable
    canonicalAvailable.value = available
    canonicalComplete.value = data.canonicalComplete
    if (available === false) {
      failedTranscriptRequest = { key, before: before ?? null }
      if (before != null) return false
    }

    if (available !== false) failedTranscriptRequest = null
    const page = [...data.messages]
    if (available !== false) {
      const nextOldestCursor = data.oldestCursor
      hasEarlier.value = data.hasMore
        && (before == null || nextOldestCursor !== String(before))
      oldestCursor.value = nextOldestCursor
    }
    beforeApply?.()
    if (before != null) {
      const seen = new Set(messages.value.map(transcriptMessageKey))
      messages.value = [
        ...page.filter(msg => !seen.has(transcriptMessageKey(msg))),
        ...messages.value,
      ]
    } else {
      messages.value = page
    }
    return available !== false
  }

  async function load(key: string) {
    const seq = ++requestSeq
    activeController?.abort()
    const controller = new AbortController()
    activeController = controller
    currentKey = key
    loading.value = true
    loadingEarlier.value = false
    transcriptError.value = false
    loadEarlierError.value = false
    canonicalAvailable.value = null
    canonicalComplete.value = null
    preview.value = null
    messages.value = []
    hasEarlier.value = false
    oldestCursor.value = null
    failedTranscriptRequest = null
    loadedEarlierCursors.clear()
    const [, transcript] = await Promise.allSettled([
      fetchPreview(key, seq, controller.signal),
      fetchTranscript(key, seq, controller.signal),
    ])
    if (seq === requestSeq && transcript.status === 'rejected') {
      transcriptError.value = true
    }
    if (seq === requestSeq) loading.value = false
  }

  async function requestEarlier(cursor: string | number, beforeApply?: () => void) {
    if (loadingEarlier.value || loading.value || !currentKey) return
    const seq = requestSeq
    loadingEarlier.value = true
    loadEarlierError.value = false
    try {
      const signal = activeController?.signal ?? new AbortController().signal
      const applied = await fetchTranscript(currentKey, seq, signal, cursor, beforeApply)
      if (seq === requestSeq && applied === true) {
        loadedEarlierCursors.add(String(cursor))
      }
    } catch {
      if (seq === requestSeq) loadEarlierError.value = true
    } finally {
      if (seq === requestSeq) loadingEarlier.value = false
    }
  }

  function loadEarlier(beforeApply?: () => void) {
    if (!hasEarlier.value || loadingEarlier.value || loading.value || !currentKey) return
    const cursor = oldestCursor.value
    if (cursor == null || loadedEarlierCursors.has(String(cursor))) return
    return requestEarlier(cursor, beforeApply)
  }

  function retryHistory(beforeApply?: () => void) {
    const failed = failedTranscriptRequest
    if (failed?.key === currentKey && failed.before != null) {
      return requestEarlier(failed.before, beforeApply)
    }
    if (canonicalAvailable.value === false) {
      return currentKey ? load(currentKey) : undefined
    }
    return loadEarlier(beforeApply)
  }

  function reset() {
    requestSeq++
    activeController?.abort()
    activeController = null
    currentKey = ''
    preview.value = null
    messages.value = []
    hasEarlier.value = false
    oldestCursor.value = null
    failedTranscriptRequest = null
    loadedEarlierCursors.clear()
    loading.value = false
    loadingEarlier.value = false
    loadEarlierError.value = false
    transcriptError.value = false
    canonicalAvailable.value = null
    canonicalComplete.value = null
  }

  return {
    preview,
    messages,
    hasEarlier,
    loading,
    loadingEarlier,
    loadEarlierError,
    transcriptError,
    canonicalAvailable,
    canonicalComplete,
    oldestCursor,
    load,
    loadEarlier,
    retryHistory,
    reset,
  }
}
