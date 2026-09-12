import type { UsageSession } from '@/types/usage'

const INTERNAL_TASK_ID = /^(?:agent|channel|cron|session|task):/i

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function usageTaskKey(row: UsageSession): string {
  return text(row.sessionKey) || text(row.session)
}

export function isUsableTaskName(value: unknown, taskKey = ''): value is string {
  const candidate = text(value)
  if (!candidate || candidate === taskKey || INTERNAL_TASK_ID.test(candidate)) return false
  return true
}

export function usageTaskDisplayName(
  row: UsageSession,
  taskTitles: ReadonlyMap<string, string>,
  fallback: string,
): string {
  const key = usageTaskKey(row)
  const directCandidates = [
    row.taskName,
    row.title,
    row.displayName,
    row.subject,
    row.derivedTitle,
  ]
  const direct = directCandidates.find(candidate => isUsableTaskName(candidate, key))
  if (typeof direct === 'string') return direct.trim()

  const mapped = taskTitles.get(key)
  return isUsableTaskName(mapped, key) ? mapped.trim() : fallback
}
