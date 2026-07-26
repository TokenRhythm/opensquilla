import { computed, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { SessionRow, SortedRow } from '@/types/usage'

const t = i18n.global.t

export function useUsageSessionRows(options: {
  visibleSessions: ComputedRef<SessionRow[]>
  rangeHiddenHint: ComputedRef<string>
  sortCol: Ref<string>
  sortAsc: Ref<boolean>
  rowVal: (row: Record<string, unknown>, ...keys: string[]) => unknown
  numericRowVal: (row: Record<string, unknown>, ...keys: string[]) => number | null
  sessionTimestamp: (row: SessionRow) => number | null
  relTime: (timestamp: number | string) => string
  sortVal: (row: SessionRow, key: string) => string | number
}) {
  const sortedRows = computed((): SortedRow[] => {
    const sorted = [...options.visibleSessions.value].sort((a, b) => {
      let va = options.sortVal(a, options.sortCol.value)
      let vb = options.sortVal(b, options.sortCol.value)
      if (typeof va === 'string') va = va.toLowerCase()
      if (typeof vb === 'string') vb = vb.toLowerCase()
      const cmp = va < vb ? -1 : va > vb ? 1 : 0
      return options.sortAsc.value ? cmp : -cmp
    })

    return sorted.map(row => {
      const sessionKey = (options.rowVal(row, 'sessionKey', 'key') || '') as string
      const sessionLabel = (
        options.rowVal(row, 'session', 'sessionKey', 'sessionId', 'session_id', 'key') || '-'
      ) as string
      const cost = options.rowVal(row, 'cost_usd', 'costUsd')
      const timestamp = options.sessionTimestamp(row)
      const modified = timestamp != null ? options.relTime(timestamp) : '-'
      const bd = row.modelBreakdown
      const hasModelBreakdown = !!(bd && bd.length > 1)

      return {
        raw: row,
        sessionKey,
        sessionLabel,
        rowIdentity: sessionKey || sessionLabel,
        modified,
        inputTokens: options.numericRowVal(row, 'input_tokens', 'inputTokens'),
        outputTokens: options.numericRowVal(row, 'output_tokens', 'outputTokens'),
        cacheReadTokens: options.numericRowVal(row, 'cache_read_tokens', 'cacheReadTokens'),
        cacheWriteTokens: options.numericRowVal(row, 'cache_write_tokens', 'cacheWriteTokens'),
        cost: cost != null ? Number(cost) : null,
        hasModelBreakdown,
      }
    })
  })

  const sessionsMeta = computed(() => {
    const n = sortedRows.value.length
    return [t('usageLogs.sessions.count', { count: n }), options.rangeHiddenHint.value].filter(Boolean).join(' · ')
  })

  return { sortedRows, sessionsMeta }
}
