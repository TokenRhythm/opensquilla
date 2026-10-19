import { nextTick, watch, type Ref } from 'vue'
import { copySelectedSkills, isSelectedSkills, sameSelectedSkills, type SelectedSkillRef } from '@/types/selectedSkills'
import { composeLocalPathText } from './useLocalPathDraft'

const DRAFT_KEY_PREFIX = 'opensquilla.chat.draft:'
export const RECENT_DRAFT_SESSION_KEY = 'opensquilla.chat.recent-draft-session'
// Cap what we persist so a giant paste cannot bloat localStorage; the composer
// itself is unbounded, only the saved copy is capped.
const MAX_DRAFT_CHARS = 100_000
const MAX_LOCAL_PATH_CHARS = 32_768

function isDraftLocalPaths(value: unknown, text: string): value is string[] {
  return Array.isArray(value) && value.length <= MAX_DRAFT_CHARS
    && value.every(path => typeof path === 'string' && path.length > 0
      && path.length <= MAX_LOCAL_PATH_CHARS && path.trim() === path && !/[\u0000-\u001f\u007f]/.test(path))
    && composeLocalPathText(text, value).length <= MAX_DRAFT_CHARS
}

/** Editor-only metadata; stable skill identities remain the send contract. */
function draftSkillPositions(value: unknown, text: string, skills: readonly SelectedSkillRef[]): Record<string, number> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const positions = value as Record<string, unknown>
  return Object.fromEntries(skills.flatMap(skill => {
    const position = Object.prototype.hasOwnProperty.call(positions, skill.instanceId)
      ? positions[skill.instanceId] : undefined
    return typeof position === 'number' && Number.isInteger(position) && position >= 0 && position <= text.length
      ? [[skill.instanceId, position]] : []
  }))
}

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

export type ChatDraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

/** A temporary conversation keeps drafts only for its mounted owner lifetime. */
export function createMemoryChatDraftStorage(): ChatDraftStorage {
  const drafts = new Map<string, string>()
  return {
    getItem: key => drafts.get(key) ?? null,
    setItem: (key, value) => { drafts.set(key, value) },
    removeItem: key => { drafts.delete(key) },
  }
}

function clearRecentDraftPointer(key?: string, storage: ChatDraftStorage | null = localStorage): void {
  try {
    const current = storage?.getItem(RECENT_DRAFT_SESSION_KEY)
    if (!key || current === key) storage?.removeItem(RECENT_DRAFT_SESSION_KEY)
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

/** Return a specific recoverable draft without changing the recent pointer. */
export function recoverableDraftSessionKey(key: string, hasAttachmentDraft = false): string {
  try {
    // The existing per-tab draft history record may point to attachment-only
    // content. This recovers a key, never storage ownership or file authority.
    return validDraftSessionKey(key) && (hasAttachmentDraft || Boolean(localStorage.getItem(draftKey(key))))
      ? key
      : ''
  } catch {
    return ''
  }
}

export interface UseChatDraftPersistenceOptions {
  sessionKey: Ref<string>
  inputText: Ref<string>
  selectedSkills?: Ref<SelectedSkillRef[]>
  skillPositions?: Ref<Record<string, number>>
  localPaths?: Ref<string[]>
  /** Undefined uses durable browser drafts; null disables persistence. */
  storage?: ChatDraftStorage | null
}

/**
 * Persist the composer draft per session to ``localStorage`` so a page refresh,
 * a session switch, or a crash before the backend accepts a send cannot
 * silently lose typed text (issue 248). The draft is keyed by session key, restored
 * when a session becomes active, and cleared once the composer is emptied
 * (i.e. after the message is sent).
 *
 * Text, explicit local paths and skill references travel together. Attachments and the
 * pending queue retain their own persistence. Storage failures are swallowed.
 */
export function useChatDraftPersistence(options: UseChatDraftPersistenceOptions) {
  let pendingRebind: { from: string; to: string; retainSource?: boolean } | null = null
  let retainedRebind: { from: string; to: string; snapshot: string | null } | null = null
  const draftStorage = () => options.storage === undefined ? localStorage : options.storage

  /** Move one proven fresh draft without loading another session's composer. */
  function rebindCurrentDraft(key: string, rebindOptions?: { retainSource?: boolean }): void {
    const previous = options.sessionKey.value
    if (!previous || !key || previous === key) return
    const from = pendingRebind?.to === previous ? pendingRebind.from : previous
    pendingRebind = { from, to: key, retainSource: rebindOptions?.retainSource }
    options.sessionKey.value = key
  }

  /** Release a crash fallback only after its new route has actually committed. */
  function completeDraftRebind(from: string, to: string): void {
    const retained = retainedRebind
    if (!retained || retained.from !== from || retained.to !== to || options.sessionKey.value !== to) return
    retainedRebind = null
    try {
      // Another tab may have edited the source while navigation was pending.
      if (draftStorage()?.getItem(draftKey(from)) === retained.snapshot) clearDraft(from)
    } catch { /* The retained source remains a safe local draft. */ }
  }

  function saveDraft(key: string, text: string, selectedSkills: SelectedSkillRef[] = [], localPaths: readonly string[] = [], skillPositions: Record<string, number> = {}): void {
    if (!key) return
    try {
      const storage = draftStorage()
      if (!storage) return
      if (text || selectedSkills.length || localPaths.length) {
        // Keep the existing capped text fallback if a draft outgrows metadata
        // limits; never silently drop just its referenced paths.
        const paths = isDraftLocalPaths(localPaths, text) ? [...localPaths] : []
        const trimmed = (paths.length ? text : composeLocalPathText(text, localPaths)).slice(0, MAX_DRAFT_CHARS)
        const positions = draftSkillPositions(skillPositions, trimmed, selectedSkills)
        storage.setItem(draftKey(key), selectedSkills.length || paths.length
          ? JSON.stringify({ version: 1, text: trimmed, selectedSkills,
            ...(paths.length ? { localPaths: paths } : {}),
            ...(Object.keys(positions).length ? { skillPositions: positions } : {}),
          }) : trimmed)
        storage.setItem(RECENT_DRAFT_SESSION_KEY, key)
      } else {
        storage.removeItem(draftKey(key))
        clearRecentDraftPointer(key, storage)
      }
    } catch {
      // Ignore storage failures in private or restricted contexts.
    }
  }

  function loadDraft(key: string): string {
    return loadDraftPayload(key).text
  }

  function loadDraftPayload(key: string): { text: string; selectedSkills: SelectedSkillRef[]; localPaths: string[]; skillPositions: Record<string, number> } {
    const empty = { text: '', selectedSkills: [] as SelectedSkillRef[], localPaths: [] as string[], skillPositions: {} }
    if (!key) return empty
    try {
      const raw = draftStorage()?.getItem(draftKey(key)) || ''
      try {
        const value = JSON.parse(raw)
        if (value?.version === 1 && typeof value.text === 'string' && isSelectedSkills(value.selectedSkills)
          && (value.localPaths === undefined || isDraftLocalPaths(value.localPaths, value.text))) {
          return { text: value.text, selectedSkills: copySelectedSkills(value.selectedSkills), localPaths: [...(value.localPaths ?? [])],
            skillPositions: draftSkillPositions(value.skillPositions, value.text, value.selectedSkills) }
        }
      } catch { /* Legacy plain-text draft. */ }
      return { ...empty, text: raw }
    } catch {
      return empty
    }
  }

  function restoreDraft(key: string): void {
    const saved = loadDraftPayload(key)
    options.inputText.value = options.localPaths ? saved.text : composeLocalPathText(saved.text, saved.localPaths)
    if (options.selectedSkills) options.selectedSkills.value = saved.selectedSkills
    if (options.skillPositions) options.skillPositions.value = saved.skillPositions
    if (options.localPaths) options.localPaths.value = saved.localPaths
  }

  async function consumeAcceptedDraft(
    key: string,
    accepted: { text: string; selectedSkills: readonly SelectedSkillRef[]; localPathReferences?: readonly string[] },
  ): Promise<void> {
    // Session switching writes the outgoing composer in a Vue watcher. Wait
    // for that write before consuming an offscreen request's saved snapshot.
    await nextTick()
    if (options.sessionKey.value === key) return
    const saved = loadDraftPayload(key)
    if (composeLocalPathText(saved.text, saved.localPaths) === accepted.text.slice(0, MAX_DRAFT_CHARS)
      && (accepted.localPathReferences === undefined
        || JSON.stringify(saved.localPaths) === JSON.stringify(accepted.localPathReferences))
      && sameSelectedSkills(saved.selectedSkills, accepted.selectedSkills)) clearDraft(key)
  }

  function clearDraft(key: string): void {
    if (!key) return
    try {
      const storage = draftStorage()
      storage?.removeItem(draftKey(key))
      clearRecentDraftPointer(key, storage)
    } catch {
      // Ignore.
    }
  }

  function discardRecentDraft(): void {
    try {
      const storage = draftStorage()
      const key = storage?.getItem(RECENT_DRAFT_SESSION_KEY) || ''
      if (validDraftSessionKey(key)) storage?.removeItem(draftKey(key))
      storage?.removeItem(RECENT_DRAFT_SESSION_KEY)
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
      const rebind = pendingRebind
      pendingRebind = null
      if (rebind && rebind.from === previousKey && rebind.to === key) {
        // Read text at watcher execution, not at Hello: typing may continue
        // before Vue flushes. In-memory preservation also works without storage.
        if (rebind.retainSource) {
          saveDraft(previousKey, options.inputText.value, options.selectedSkills?.value, options.localPaths?.value, options.skillPositions?.value)
          try { retainedRebind = { from: previousKey, to: key, snapshot: draftStorage()?.getItem(draftKey(previousKey)) ?? null } }
          catch { retainedRebind = null }
        } else clearDraft(previousKey)
        saveDraft(key, options.inputText.value, options.selectedSkills?.value, options.localPaths?.value, options.skillPositions?.value)
        return
      }
      if (previousKey && previousKey !== key) {
        saveDraft(previousKey, options.inputText.value, options.selectedSkills?.value, options.localPaths?.value, options.skillPositions?.value)
        restoreDraft(key)
        return
      }
      if (!key) return
      if (options.inputText.value || options.selectedSkills?.value.length || options.localPaths?.value.length) return
      restoreDraft(key)
    },
    // Bind draft ownership before queued input-save watchers or route resets
    // run. Otherwise an edit followed by navigation in the same tick can save
    // A's text under B's key and overwrite B before its draft is restored.
    { immediate: true, flush: 'sync' },
  )

  // Persist on every composer change for the CURRENT session. Empty text clears
  // the saved draft (the send path empties inputText, so this doubles as the
  // "sent → forget the draft" hook).
  watch([options.inputText, () => options.selectedSkills?.value, () => options.localPaths?.value, () => options.skillPositions?.value], ([text]) => {
    saveDraft(options.sessionKey.value, text, options.selectedSkills?.value, options.localPaths?.value, options.skillPositions?.value)
  }, { deep: true })

  return { saveDraft, loadDraft, clearDraft, discardRecentDraft, rebindCurrentDraft, completeDraftRebind, consumeAcceptedDraft }
}
