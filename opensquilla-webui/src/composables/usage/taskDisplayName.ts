import type { SessionRow } from '@/types/usage'

const INTERNAL_TASK_ID = /^(?:agent|channel|cron|session|task):/i

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function usageTaskKey(row: SessionRow): string {
  return text(row.sessionKey) || text(row.key) || text(row.session)
}

export function isUsableTaskName(value: unknown, taskKey = ''): value is string {
  const candidate = text(value)
  if (!candidate || candidate === taskKey || INTERNAL_TASK_ID.test(candidate)) return false
  return true
}

export function usageTaskDisplayName(
  row: SessionRow,
  taskTitles: ReadonlyMap<string, string>,
  fallback: string,
): string {
  const key = usageTaskKey(row)
  const directCandidates = [
    row.taskName,
    row.task_name,
    row.title,
    row.displayName,
    row.display_name,
    row.subject,
    row.derivedTitle,
    row.derived_title,
  ]
  const direct = directCandidates.find(candidate => isUsableTaskName(candidate, key))
  if (typeof direct === 'string') return direct.trim()

  const mapped = taskTitles.get(key)
  return isUsableTaskName(mapped, key) ? mapped.trim() : fallback
}
