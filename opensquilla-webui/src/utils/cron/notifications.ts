const recentlyNotified = new Map<string, number>()
const RECENT_WINDOW_MS = 15_000
const REMINDER_PREVIEW_MAX_CHARS = 120

export function reminderToastPreview(value: unknown): string {
  const normalized = typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : ''
  if (normalized.length <= REMINDER_PREVIEW_MAX_CHARS) return normalized
  return `${normalized.slice(0, REMINDER_PREVIEW_MAX_CHARS - 1).trimEnd()}…`
}

export function markCronFinishNotified(runId: string, now = Date.now()): void {
  if (!runId) return
  recentlyNotified.set(runId, now)
  for (const [id, timestamp] of recentlyNotified) {
    if (now - timestamp > RECENT_WINDOW_MS) recentlyNotified.delete(id)
  }
}

export function wasCronFinishNotified(runId: string, now = Date.now()): boolean {
  const timestamp = recentlyNotified.get(runId)
  return timestamp !== undefined && now - timestamp <= RECENT_WINDOW_MS
}
