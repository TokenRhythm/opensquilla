const recentlyNotified = new Map<string, number>()
const RECENT_WINDOW_MS = 15_000

export function markCronFinishNotified(jobId: string, now = Date.now()): void {
  if (!jobId) return
  recentlyNotified.set(jobId, now)
  for (const [id, timestamp] of recentlyNotified) {
    if (now - timestamp > RECENT_WINDOW_MS) recentlyNotified.delete(id)
  }
}

export function wasCronFinishNotified(jobId: string, now = Date.now()): boolean {
  const timestamp = recentlyNotified.get(jobId)
  return timestamp !== undefined && now - timestamp <= RECENT_WINDOW_MS
}
