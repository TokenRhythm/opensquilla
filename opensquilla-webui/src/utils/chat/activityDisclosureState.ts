const expandedByKey = new Map<string, boolean>()
const durationByKey = new Map<string, number>()

export function readAssistantActivityExpansion(
  key: string,
  fallback: boolean,
  continuityKey = '',
): boolean {
  if (key && expandedByKey.has(key)) return expandedByKey.get(key) === true
  if (continuityKey && expandedByKey.has(continuityKey)) {
    return expandedByKey.get(continuityKey) === true
  }
  return fallback
}

export function writeAssistantActivityExpansion(
  key: string,
  expanded: boolean,
  continuityKey = '',
): void {
  if (key) expandedByKey.set(key, expanded)
  if (continuityKey) expandedByKey.set(continuityKey, expanded)
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
}
