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
interface ActivityOwner { sessionKey: string; scope: string | null }
const DEFAULT_OWNER = Symbol('route-activity-owner')
const activeOwners = new Map<symbol, ActivityOwner>()
const retainedScopes = new Map<string, string>()

function keyBelongsToSession(key: string, sessionKey: string): boolean {
  try {
    const identity: unknown = JSON.parse(key)
    return Array.isArray(identity) && identity[0] === sessionKey
  } catch {
    return false
  }
}

function clearLiveSession(sessionKey: string): void {
  for (const key of expandedByKey.keys()) {
    if (keyBelongsToSession(key, sessionKey)) expandedByKey.delete(key)
  }
  for (const key of durationByKey.keys()) {
    if (keyBelongsToSession(key, sessionKey)) durationByKey.delete(key)
  }
}

function retireInactiveSession(previous: ActivityOwner, preserve: boolean): void {
  const { sessionKey, scope } = previous
  if ([...activeOwners.values()].some(owner => owner.sessionKey === sessionKey)) return
  const choices = preserve && scope
    ? [...expandedByKey].filter(([key, state]) => state.manual && keyBelongsToSession(key, sessionKey))
      .sort((a, b) => a[1].version - b[1].version).slice(-MAX_MANUAL_CHOICES_PER_SESSION)
    : []
  manualChoicesBySession.delete(sessionKey)
  if (choices.length && scope) {
    manualChoicesBySession.set(sessionKey, new Map(choices))
    retainedScopes.set(sessionKey, scope)
  } else retainedScopes.delete(sessionKey)
  clearLiveSession(sessionKey)
}

/**
 * Each mounted conversation owns its active session independently. Navigation
 * retires only that owner's previous session, never a simultaneously visible
 * main/side surface. Inactive retention keeps only explicit reader choices.
 */
export function activateAssistantActivitySession(
  sessionKey: string,
  scope: string | null | undefined,
  ownerToken = DEFAULT_OWNER,
): void {
  const nextScope = scope || null
  const previous = activeOwners.get(ownerToken)
  if (previous?.scope === nextScope && previous.sessionKey === sessionKey) return
  activeOwners.delete(ownerToken)
  if (previous) {
    retireInactiveSession(previous, previous.scope === nextScope)
    if (previous.scope !== nextScope) {
      // Retained choices from this authenticated namespace must not survive
      // a delivery identity change. Other mounted owners remain untouched.
      for (const [key, retainedScope] of retainedScopes) {
        if (retainedScope === previous.scope) {
          manualChoicesBySession.delete(key)
          retainedScopes.delete(key)
        }
      }
    }
  }
  const alreadyActive = [...activeOwners.values()].some(owner => owner.sessionKey === sessionKey
    && owner.scope === nextScope)
  activeOwners.set(ownerToken, { sessionKey, scope: nextScope })
  if (alreadyActive) return
  clearLiveSession(sessionKey)
  const choices = nextScope && retainedScopes.get(sessionKey) === nextScope
    ? manualChoicesBySession.get(sessionKey) : undefined
  // The active session is kept in the ordinary live map; the bounded cache
  // contains only inactive sessions. Reading a session refreshes its LRU age.
  manualChoicesBySession.delete(sessionKey)
  retainedScopes.delete(sessionKey)
  if (choices) for (const [key, state] of choices) expandedByKey.set(key, state)
  while (manualChoicesBySession.size >= MAX_RETAINED_SESSIONS) {
    const oldest = manualChoicesBySession.keys().next().value
    if (oldest === undefined) break
    manualChoicesBySession.delete(oldest)
    retainedScopes.delete(oldest)
  }
}

export function releaseAssistantActivitySession(ownerToken = DEFAULT_OWNER): void {
  const previous = activeOwners.get(ownerToken)
  activeOwners.delete(ownerToken)
  if (previous) retireInactiveSession(previous, true)
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
    retainedScopes.delete(sessionKey)
    clearLiveSession(sessionKey)
    return
  }
  expandedByKey.clear()
  durationByKey.clear()
  manualChoicesBySession.clear()
  retainedScopes.clear()
  expansionVersion = 0
}
