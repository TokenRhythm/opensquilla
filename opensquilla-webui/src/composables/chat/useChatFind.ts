import { computed, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import type { ChatRouteHeaderFindState } from './useChatRouteHeaderBridge'
import { SessionMessageSearchError, type SessionMessageSearch, type SessionMessageSearchMatch } from '@/modules/sessionMessageSearch'

/** Owns only the find operation; it never replaces the session's live read lease. */
export function useChatFind(options: {
  sessionKey: Readonly<Ref<string>>
  identity: Readonly<Ref<string | null>>
  foregroundBusy?: Readonly<Ref<boolean>>
  search: SessionMessageSearch | null
  onOpen: () => void
  onReset: (restore: boolean) => void
  onClear: () => void
  onSelect: (match: SessionMessageSearchMatch, occurrence: number, context: {
    query: string; sessionId: string; sessionEpoch: number; signal: AbortSignal
  }) => Promise<void>
}) {
  const open = ref(false)
  const query = ref('')
  const resultQuery = ref('')
  const matches = shallowRef<SessionMessageSearchMatch[]>([])
  const index = ref(-1)
  const status = ref<ChatRouteHeaderFindState['status']>('idle')
  const focusSequence = ref(0)
  const complete = ref(true)
  const coverageComplete = ref(true)
  let operation = 0
  let controller: AbortController | null = null
  let selection: AbortController | null = null
  let resumeScan: (() => void) | undefined
  let pendingNext = false
  const state = computed<ChatRouteHeaderFindState>(() => ({
    open: open.value, query: query.value, resultQuery: resultQuery.value,
    matchCount: matches.value.length, activeMatch: index.value + 1,
    status: status.value, focusSequence: focusSequence.value,
    complete: complete.value, coverageComplete: coverageComplete.value,
  }))

  function abort() {
    operation += 1
    controller?.abort()
    selection?.abort()
    controller = null
    selection = null
    resumeScan = undefined
    pendingNext = false
  }
  function show() {
    if (!open.value) { options.onOpen(); open.value = true }
    focusSequence.value += 1
  }
  function close(restore = true) {
    const wasOpen = open.value
    abort()
    open.value = false
    query.value = resultQuery.value = ''
    matches.value = []
    index.value = -1
    status.value = 'idle'
    complete.value = coverageComplete.value = true
    if (wasOpen) options.onReset(restore)
  }
  function updateQuery(value: string) {
    if (value === query.value) return
    abort()
    query.value = value
    options.onClear()
    matches.value = []
    index.value = -1
    status.value = value.trim() ? 'loading' : 'idle'
    complete.value = !value.trim()
  }

  function captureAcceptedSend(returnToLive: () => void): () => void {
    const wasOpen = open.value
    const run = operation
    const focus = focusSequence.value
    const key = options.sessionKey.value
    const identity = options.identity.value
    return () => {
      // An explicit send can finish after the user switches or starts a new
      // search. Only the search active at submission yields to its accepted turn.
      if (!wasOpen || !open.value || run !== operation || focus !== focusSequence.value
        || key !== options.sessionKey.value || identity !== options.identity.value) return
      close(false)
      returnToLive()
    }
  }

  async function search(value: string) {
    abort()
    query.value = value
    resultQuery.value = value
    matches.value = []
    index.value = -1
    options.onClear()
    const literal = value.trim()
    if (!literal || !open.value) { status.value = 'idle'; complete.value = true; return }
    if (!options.search) { status.value = 'error'; return }
    const run = operation
    const key = options.sessionKey.value
    controller = new AbortController()
    const signal = controller.signal
    const current = () => run === operation && !signal.aborted
    status.value = 'loading'
    complete.value = false
    coverageComplete.value = true
    let sessionId: string | undefined
    let sessionEpoch: number | undefined
    let cursor: string | undefined
    let unavailable = false
    let presentedIndex = -1
    const seen = new Set<string>()

    async function select(next: number) {
      if (!current()) return
      const match = matches.value[next]
      if (!match || sessionId === undefined || sessionEpoch === undefined) return
      selection?.abort()
      selection = new AbortController()
      const selectedSignal = selection.signal
      const stop = () => selection?.abort()
      signal.addEventListener('abort', stop, { once: true })
      index.value = next
      const occurrence = matches.value.slice(0, next).filter(item => item.messageId === match.messageId).length
      try {
        for (let retry = 0; current() && !selectedSignal.aborted; retry++) {
          try {
            await options.onSelect(match, occurrence, { query: literal, sessionId, sessionEpoch, signal: selectedSignal })
            if (current() && !selectedSignal.aborted) presentedIndex = next
            break
          } catch (error) {
            if (!current() || selectedSignal.aborted) return
            if (!(error instanceof SessionMessageSearchError) || error.reason !== 'busy') throw error
            if (retry >= 2) { index.value = presentedIndex; return }
            await wait(Math.max(error.retryAfterMs ?? 0, 250 * 2 ** retry + Math.random() * 100), selectedSignal)
          }
        }
      } catch {
        if (current() && !selectedSignal.aborted) status.value = 'error'
      } finally { signal.removeEventListener('abort', stop) }
    }
    selectMatch = select
    function pause() {
      // No background sweep: the next explicit navigation resumes this cursor.
      resumeScan = () => { resumeScan = undefined; void scan() }
      if (status.value !== 'error') status.value = 'ready'
    }
    function wait(ms: number, waitSignal = signal) {
      return new Promise<void>(resolve => {
        if (!current() || waitSignal.aborted) { resolve(); return }
        const timer = setTimeout(done, ms)
        function done() { clearTimeout(timer); waitSignal.removeEventListener('abort', done); resolve() }
        waitSignal.addEventListener('abort', done, { once: true })
      })
    }
    async function scan() {
      const started = Date.now()
      const initialCount = matches.value.length
      let pages = 0
      let busyRetries = 0
      try {
        do {
          if ((pages > 0 || busyRetries > 0) && Date.now() - started >= (options.foregroundBusy?.value ? 800 : 2000)) {
            pause(); return
          }
          let page
          try {
            page = await options.search!.search({ key, query: literal, sessionId, sessionEpoch, cursor, limit: 100, signal })
          } catch (error) {
            if (!current()) return
            if (!(error instanceof SessionMessageSearchError) || error.reason !== 'busy') throw error
            // Contention is ordinary backpressure, not a broken conversation.
            // Retries remain bounded across the whole batch and keep its cursor.
            if (busyRetries >= 2 || Date.now() - started >= 2000) { pause(); return }
            await wait(Math.max(error.retryAfterMs ?? 0, 250 * 2 ** busyRetries++ + Math.random() * 100))
            continue
          }
          if (!current()) return
          sessionId = page.sessionId
          sessionEpoch = page.sessionEpoch
          unavailable ||= page.unavailableEntries > 0
          // Archive coverage is proved incrementally; a provisional false may
          // become true. An actual unavailable body remains a permanent gap.
          coverageComplete.value = page.coverageComplete && !unavailable
          const next = page.matches.filter(match => {
            const id = `${match.messageId}:${match.contentRevision}:${match.offsetUtf16}`
            if (seen.has(id)) return false
            seen.add(id)
            return true
          })
          matches.value = [...matches.value, ...next]
          complete.value = page.complete
          cursor = page.nextCursor ?? undefined
          if (matches.value.length) {
            if (status.value !== 'error') status.value = 'ready'
            if (index.value < 0) await select(0)
            else if (pendingNext && index.value + 1 < matches.value.length) {
              pendingNext = false
              await select(index.value + 1)
            }
          }
          if (!current()) return
          if (!cursor || page.complete) {
            if (pendingNext && matches.value.length) { pendingNext = false; await select(0) }
            break
          }
          const busy = options.foregroundBusy?.value === true
          if (++pages >= (busy ? 4 : 20) || Date.now() - started >= (busy ? 800 : 2000)
            || matches.value.length - initialCount >= 500) { pause(); return }
          await wait(busy ? 250 : 100)
        } while (current())
        if (current() && status.value !== 'error') status.value = 'ready'
      } catch {
        if (current()) { status.value = 'error'; complete.value = false }
      }
    }
    await scan()
  }
  let selectMatch: (index: number) => Promise<void> = async () => {}
  function navigate(direction: 'previous' | 'next') {
    if (!open.value || resultQuery.value !== query.value || status.value !== 'ready') return
    pendingNext = false
    const count = matches.value.length
    if (!count) { resumeScan?.(); return }
    if (direction === 'next' && index.value === count - 1 && !complete.value) {
      pendingNext = true
      resumeScan?.()
      return
    }
    const next = index.value < 0 ? (direction === 'next' ? 0 : count - 1)
      : (index.value + (direction === 'next' ? 1 : -1) + count) % count
    void selectMatch(next)
  }
  watch([options.sessionKey, options.identity], () => close(false), { flush: 'sync' })
  onScopeDispose(() => close(false))
  return { state, open, query, resultQuery, matches, index, show, close, updateQuery, search, navigate, captureAcceptedSend }
}
