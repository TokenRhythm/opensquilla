interface ExpansionState {
  expanded: boolean
  version: number
}

const expandedByKey = new Map<string, ExpansionState>()
const durationByKey = new Map<string, number>()
let expansionVersion = 0

export function readAssistantActivityExpansion(
  key: string,
  fallback: boolean,
  continuityKey = '',
): boolean {
  const keyed = key ? expandedByKey.get(key) : undefined
  const continuous = continuityKey ? expandedByKey.get(continuityKey) : undefined
  // Canonical reconciliation can remount a turn with an older message key.
  // Prefer the most recently written state across that key and the stable turn
  // identity so the remount cannot resurrect an earlier auto-collapse.
  if (continuous && (!keyed || continuous.version > keyed.version)) {
    return continuous.expanded
  }
  if (keyed) return keyed.expanded
  if (continuous) return continuous.expanded
  return fallback
}

export function writeAssistantActivityExpansion(
  key: string,
  expanded: boolean,
  continuityKey = '',
): void {
  const state = { expanded, version: ++expansionVersion }
  if (key) expandedByKey.set(key, state)
  if (continuityKey) expandedByKey.set(continuityKey, state)
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
