interface ExpansionState {
  expanded: boolean
  manual: boolean
  version: number
}

const expandedByKey = new Map<string, ExpansionState>()
const durationByKey = new Map<string, number>()
let expansionVersion = 0

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

export function clearAssistantActivityExpansionState(): void {
  expandedByKey.clear()
  durationByKey.clear()
  expansionVersion = 0
}
