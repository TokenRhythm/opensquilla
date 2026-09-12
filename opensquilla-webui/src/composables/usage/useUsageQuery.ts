import type {
  Observability,
  UsageSnapshotOptions,
} from '@/modules/observability'
import type {
  UsageRangeSelection,
  UsageSnapshot,
} from '@/types/usage'

export type UsageQueryOptions = UsageSnapshotOptions

export function naturalRangeStartMs(
  range: UsageRangeSelection,
  now: Date = new Date(),
): number | null {
  if (range === 'all') return null
  const days = range === 'today' ? 1 : Number(range)
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  start.setDate(start.getDate() - (days - 1))
  return start.getTime()
}

export function requestUsageSnapshot(
  observability: Observability,
  range: UsageRangeSelection,
  options: UsageQueryOptions = {},
): Promise<UsageSnapshot> {
  return observability.usage(range, options)
}
