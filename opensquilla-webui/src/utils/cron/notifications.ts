const recentlyNotified = new Map<string, number>()
const RECENT_WINDOW_MS = 15_000

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
