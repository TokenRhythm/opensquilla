import type { TaskProgressSnapshot } from '@/types/taskProgress'

export function normalizeTaskProgress(value: unknown): TaskProgressSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (typeof source.revision !== 'number' || !Number.isSafeInteger(source.revision)
    || source.revision < 1 || !Array.isArray(source.steps)) return null
  const steps: TaskProgressSnapshot['steps'] = []
  for (const item of source.steps) {
    if (!item || typeof item !== 'object') return null
    const text = item.text ?? item.step
    if (typeof text !== 'string' || !text.trim()
      || !['pending', 'in_progress', 'completed'].includes(item.status)) return null
    steps.push({ text, status: item.status })
  }
  return {
    revision: source.revision,
    explanation: typeof source.explanation === 'string' ? source.explanation : null,
    steps,
  }
}
