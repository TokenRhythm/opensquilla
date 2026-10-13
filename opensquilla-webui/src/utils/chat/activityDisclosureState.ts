interface ExpansionState {
  expanded: boolean
  manual: boolean
  version: number
}

const expandedByKey = new Map<string, ExpansionState>()
const durationByKey = new Map<string, number>()
let expansionVersion = 0
const manualChoicesBySession = new Map<string, Map<string, ExpansionState>>()
const MAX_RETAINED_SESSIONS = 6
const MAX_MANUAL_CHOICES_PER_SESSION = 256
let activeSessionKey = ''
let activeScope: string | null = null

function keyBelongsToSession(key: string, sessionKey: string): boolean {
  try {
    const identity: unknown = JSON.parse(key)
    return Array.isArray(identity) && identity[0] === sessionKey
  } catch {
    return false
  }
}

/** Preserve explicit reader choices, never elapsed time or automatic live state. */
export function activateAssistantActivitySession(sessionKey: string, scope: string | null | undefined): void {
  const nextScope = scope || null
  if (nextScope === activeScope && sessionKey === activeSessionKey) return
  if (nextScope !== activeScope || !nextScope) {
    clearAssistantActivityExpansionState()
  } else if (activeSessionKey) {
    const choices = [...expandedByKey].filter(([key, state]) => state.manual
      && keyBelongsToSession(key, activeSessionKey))
      .sort((a, b) => a[1].version - b[1].version)
      .slice(-MAX_MANUAL_CHOICES_PER_SESSION)
    manualChoicesBySession.delete(activeSessionKey)
    if (choices.length) manualChoicesBySession.set(activeSessionKey, new Map(choices))
  }
  expandedByKey.clear()
  durationByKey.clear()
  activeScope = nextScope
  activeSessionKey = sessionKey
  const choices = nextScope ? manualChoicesBySession.get(sessionKey) : undefined
  // The active session is kept in the ordinary live map; the bounded cache
  // contains only inactive sessions. Reading a session refreshes its LRU age.
  manualChoicesBySession.delete(sessionKey)
  if (choices) for (const [key, state] of choices) expandedByKey.set(key, state)
  while (manualChoicesBySession.size >= MAX_RETAINED_SESSIONS) {
    const oldest = manualChoicesBySession.keys().next().value
    if (oldest === undefined) break
    manualChoicesBySession.delete(oldest)
  }
}

export function assistantActivityContinuityKey(sessionKey: string, turnKey: string): string {
  return turnKey ? JSON.stringify([sessionKey, 'assistant-activity-turn', turnKey]) : ''
}

function readExpansionState(
  key: string,
  continuityKey = '',
): ExpansionState | undefined {
  const keyed = key ? expandedByKey.get(key) : undefined
  const continuous = continuityKey ? expandedByKey.get(continuityKey) : undefined
  // Canonical reconciliation can remount a turn with an older message key.
  // Prefer the most recently written state across that key and the stable turn
  // identity so the remount cannot resurrect an earlier auto-collapse.
  if (continuous && (!keyed || continuous.version > keyed.version)) {
    return continuous
  }
  return keyed ?? continuous
}

export function readAssistantActivityExpansion(
  key: string,
  fallback: boolean,
  continuityKey = '',
): boolean {
  return readExpansionState(key, continuityKey)?.expanded ?? fallback
}

export function readAssistantActivityManualChoice(
  key: string,
  continuityKey = '',
): boolean | undefined {
  const state = readExpansionState(key, continuityKey)
  return state?.manual ? state.expanded : undefined
}

export function writeAssistantActivityExpansion(
  key: string,
  expanded: boolean,
  continuityKey = '',
  manual = false,
): void {
  const state = { expanded, manual, version: ++expansionVersion }
  if (key) expandedByKey.set(key, state)
  if (continuityKey) expandedByKey.set(continuityKey, state)
}

/** Adopt only an explicit choice when the optimistic turn receives its durable id. */
export function migrateAssistantActivityExpansion(previousKey: string, nextKey: string): void {
  if (!previousKey || !nextKey || previousKey === nextKey) return
  const previous = expandedByKey.get(previousKey)
  const current = expandedByKey.get(nextKey)
  if (!previous?.manual || (current && current.version >= previous.version)) return
  expandedByKey.set(nextKey, previous)
}

export function readAssistantActivityDuration(key: string, continuityKey = ''): number {
  if (key && durationByKey.has(key)) return durationByKey.get(key) ?? 0
  return continuityKey ? durationByKey.get(continuityKey) ?? 0 : 0
}

export function writeAssistantActivityDuration(
  key: string,
  seconds: number,
  continuityKey = '',
): void {
  if (!Number.isFinite(seconds) || seconds <= 0) return
  if (key) durationByKey.set(key, Math.floor(seconds))
  if (continuityKey) durationByKey.set(continuityKey, Math.floor(seconds))
}

export function clearAssistantActivityExpansionState(sessionKey?: string): void {
  if (sessionKey !== undefined) {
    manualChoicesBySession.delete(sessionKey)
    for (const key of expandedByKey.keys()) {
      if (keyBelongsToSession(key, sessionKey)) expandedByKey.delete(key)
    }
    for (const key of durationByKey.keys()) {
      if (keyBelongsToSession(key, sessionKey)) durationByKey.delete(key)
    }
    return
  }
  expandedByKey.clear()
  durationByKey.clear()
  manualChoicesBySession.clear()
  expansionVersion = 0
}
