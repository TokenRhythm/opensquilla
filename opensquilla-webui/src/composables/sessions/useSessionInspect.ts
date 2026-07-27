import { ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type { ChatHistoryMessage, ChatHistoryResponse } from '@/types/rpc'

// There is deliberately no sessions.get RPC; the inspect drawer composes
// sessions.preview (summary snippet) with chat.history (transcript pages).

export interface SessionInspectPreview {
  key: string
  title: string
  lastMessage: string
  updatedAt: number | null
}

interface RawPreviewRow {
  key?: string
  title?: string
  lastMessage?: string
  updatedAt?: number
}

interface SessionsPreviewResponse {
  previews?: RawPreviewRow[]
}

interface SessionsAbortResponse {
  aborted?: boolean
  key?: string
}

export const SESSION_INSPECT_PAGE_SIZE = 20

function transcriptMessageKey(msg: ChatHistoryMessage): string {
  return String(msg.message_id || msg.id || `${msg.role || ''}:${msg.timestamp ?? msg.ts ?? ''}:${msg.text || ''}`)
}

export function useSessionInspect() {
  const rpc = useRpcStore()
  const preview = ref<SessionInspectPreview | null>(null)
  const messages = ref<ChatHistoryMessage[]>([])
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
  let failedTranscriptRequest: {
    key: string
    before: string | number | null
  } | null = null
  const loadedEarlierCursors = new Set<string>()

  async function fetchPreview(key: string, seq: number) {
    try {
      const data = await rpc.call<SessionsPreviewResponse>('sessions.preview', { keys: [key] })
      if (seq !== requestSeq) return
      const rows = data?.previews || []
      const row = rows.find(item => item.key === key) || rows[0] || null
      const updatedAt = row?.updatedAt != null ? Number(row.updatedAt) : NaN
      preview.value = row
        ? {
            key: String(row.key || key),
            title: String(row.title || ''),
            lastMessage: String(row.lastMessage || ''),
            updatedAt: Number.isFinite(updatedAt) ? updatedAt : null,
          }
        : null
    } catch {
      // Preview is a summary garnish; header data falls back to the ledger
      // row and transcript failures are surfaced separately.
      if (seq === requestSeq) preview.value = null
    }
  }

  async function fetchTranscript(
    key: string,
    seq: number,
    before?: string | number | null,
    beforeApply?: () => void,
  ) {
    const params: Record<string, unknown> = {
      sessionKey: key,
      limit: SESSION_INSPECT_PAGE_SIZE,
      includeCanonical: true,
      includeSummaries: false,
    }
    if (before != null) params.before = before
    const data = await rpc.call<ChatHistoryResponse>('chat.history', params)
    if (seq !== requestSeq) return
    const available = data?.canonical_available ?? data?.canonicalAvailable
    if (typeof available === 'boolean') canonicalAvailable.value = available
    const complete = data?.canonical_complete ?? data?.canonicalComplete
    if (typeof complete === 'boolean') canonicalComplete.value = complete
    if (available === false) {
      failedTranscriptRequest = { key, before: before ?? null }
      if (before != null) return false
    }

    if (available !== false) failedTranscriptRequest = null
    const page = data?.messages || []
    const nextOldestCursor = data?.oldest_cursor ?? data?.oldestCursor ?? null
    hasEarlier.value = Boolean(data?.has_more ?? data?.hasMore)
      && (before == null || nextOldestCursor !== before)
    oldestCursor.value = nextOldestCursor
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
    try {
      await rpc.waitForConnection()
      if (seq !== requestSeq) return
      const [, transcript] = await Promise.allSettled([
        fetchPreview(key, seq),
        fetchTranscript(key, seq),
      ])
      if (seq !== requestSeq) return
      if (transcript.status === 'rejected') transcriptError.value = true
    } catch {
      if (seq === requestSeq) transcriptError.value = true
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  async function requestEarlier(cursor: string | number, beforeApply?: () => void) {
    if (loadingEarlier.value || loading.value || !currentKey) return
    const seq = requestSeq
    loadingEarlier.value = true
    loadEarlierError.value = false
    try {
      const applied = await fetchTranscript(currentKey, seq, cursor, beforeApply)
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

  async function abortSession(key: string): Promise<boolean> {
    const data = await rpc.call<SessionsAbortResponse>('sessions.abort', { key })
    return data?.aborted === true
  }

  function reset() {
    requestSeq++
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
    abortSession,
    reset,
  }
}
