import { watch, type Ref } from 'vue'

const DRAFT_KEY_PREFIX = 'opensquilla.chat.draft:'
export const RECENT_DRAFT_SESSION_KEY = 'opensquilla.chat.recent-draft-session'
// Cap what we persist so a giant paste cannot bloat localStorage; the composer
// itself is unbounded, only the saved copy is capped.
const MAX_DRAFT_CHARS = 100_000

function draftKey(key: string): string {
  return DRAFT_KEY_PREFIX + key
}

function validDraftSessionKey(key: string): boolean {
  if (!key || key.length > 512 || key !== key.trim()) return false
  if (/[/\u0000-\u001f\u007f\s]/.test(key)) return false
  const marker = ':webchat:'
  const markerIndex = key.indexOf(marker)
  return key.startsWith('agent:')
    && markerIndex > 'agent:'.length
    && markerIndex + marker.length < key.length
}

function clearRecentDraftPointer(key?: string): void {
  try {
    const current = localStorage.getItem(RECENT_DRAFT_SESSION_KEY)
    if (!key || current === key) localStorage.removeItem(RECENT_DRAFT_SESSION_KEY)
  } catch {
    // Storage can be unavailable in private or restricted contexts.
  }
}

/** Return the single recoverable draft session, retiring stale/corrupt pointers. */
export function recentDraftSessionKey(): string {
  try {
    const key = localStorage.getItem(RECENT_DRAFT_SESSION_KEY)
    if (key === null) return ''
    if (!validDraftSessionKey(key) || !localStorage.getItem(draftKey(key))) {
      localStorage.removeItem(RECENT_DRAFT_SESSION_KEY)
      return ''
    }
    return key
  } catch {
    return ''
  }
}

export interface UseChatDraftPersistenceOptions {
  sessionKey: Ref<string>
  inputText: Ref<string>
}

/**
 * Persist the composer draft per session to ``localStorage`` so a page refresh,
 * a session switch, or a crash before the backend accepts a send cannot
 * silently lose typed text (issue 248). The draft is keyed by session key, restored
 * when a session becomes active, and cleared once the composer is emptied
 * (i.e. after the message is sent).
 *
 * Deliberately minimal: it does NOT touch attachments or the pending queue —
 * only the composer text, which is the recurring "my instruction vanished"
 * complaint. Storage failures (private mode, quota) are swallowed.
 */
export function useChatDraftPersistence(options: UseChatDraftPersistenceOptions) {
  function saveDraft(key: string, text: string): void {
    if (!key) return
    try {
      if (text) {
        localStorage.setItem(draftKey(key), text.slice(0, MAX_DRAFT_CHARS))
        localStorage.setItem(RECENT_DRAFT_SESSION_KEY, key)
      } else {
        localStorage.removeItem(draftKey(key))
        clearRecentDraftPointer(key)
      }
    } catch {
      // Ignore storage failures in private or restricted contexts.
    }
  }

  function loadDraft(key: string): string {
    if (!key) return ''
    try {
      return localStorage.getItem(draftKey(key)) || ''
    } catch {
      return ''
    }
  }

  function clearDraft(key: string): void {
    if (!key) return
    try {
      localStorage.removeItem(draftKey(key))
      clearRecentDraftPointer(key)
    } catch {
      // Ignore.
    }
  }

  function discardRecentDraft(): void {
    try {
      const key = localStorage.getItem(RECENT_DRAFT_SESSION_KEY) || ''
      if (validDraftSessionKey(key)) localStorage.removeItem(draftKey(key))
      localStorage.removeItem(RECENT_DRAFT_SESSION_KEY)
    } catch {
      // Ignore storage failures in private or restricted contexts.
    }
  }

  // On session switch: persist the outgoing session's draft, then load the
  // incoming session's saved draft into the composer. On the very first
  // activation (no previous key) restore only when the composer is empty so we
  // never clobber text the user already started typing in a fresh view.
  watch(
    options.sessionKey,
    (key, previousKey) => {
      if (previousKey && previousKey !== key) {
        saveDraft(previousKey, options.inputText.value)
        options.inputText.value = loadDraft(key)
        return
      }
      if (!key) return
      if (options.inputText.value) return
      const saved = loadDraft(key)
      if (saved) options.inputText.value = saved
    },
    { immediate: true },
  )

  // Persist on every composer change for the CURRENT session. Empty text clears
  // the saved draft (the send path empties inputText, so this doubles as the
  // "sent → forget the draft" hook).
  watch(options.inputText, (text) => {
    saveDraft(options.sessionKey.value, text)
  })

  return { saveDraft, loadDraft, clearDraft, discardRecentDraft }
}
