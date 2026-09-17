import { computed, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { SortedRow, UsageSession } from '@/types/usage'

const t = i18n.global.t

function nonEmptyText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function usageRowIdentity(row: UsageSession, sessionKey: string, sessionLabel: string): string {
  return nonEmptyText(sessionKey)
    || nonEmptyText(row.sessionId)
    || nonEmptyText(row.session)
    || sessionLabel
}

export function useUsageSessionRows(options: {
  visibleSessions: ComputedRef<UsageSession[]>
  rangeHiddenHint: ComputedRef<string>
  sortCol: Ref<string>
  sortAsc: Ref<boolean>
  sessionTimestamp: (row: UsageSession) => number | null
  relTime: (timestamp: number | string) => string
  sortVal: (row: UsageSession, key: string) => string | number
  taskName: (row: UsageSession) => string
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
      const sessionKey = row.sessionKey
      const sessionLabel = options.taskName(row)
      const timestamp = options.sessionTimestamp(row)
      const modified = timestamp != null ? options.relTime(timestamp) : '-'
      const bd = row.modelBreakdown
      const hasModelBreakdown = !!(bd && bd.length > 1)

      return {
        raw: row,
        sessionKey,
        sessionLabel,
        rowIdentity: usageRowIdentity(row, sessionKey, sessionLabel),
        modified,
        inputTokens: row.inputTokens,
        outputTokens: row.outputTokens,
        cacheReadTokens: row.cacheReadTokens,
        cacheWriteTokens: row.cacheWriteTokens,
        cost: row.costUsd,
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
