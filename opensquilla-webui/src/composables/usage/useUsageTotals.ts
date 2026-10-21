import { computed, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import { nativeBillingDisplay } from '@/composables/usage/nativeBilling'
import type { UsageSession, UsageTotals } from '@/types/usage'

export function useUsageTotals(options: {
  visibleSessions: ComputedRef<UsageSession[]>
  serverTotals?: ComputedRef<UsageTotals | null>
  currency: Ref<string>
  fmtCost: (
    usd: number | null | undefined,
    opts?: { decimals?: number; source?: UsageTotals },
  ) => string
}) {
  const usageTotals = computed((): UsageTotals => {
    if (options.serverTotals?.value) return options.serverTotals.value
    const totals = options.visibleSessions.value.reduce((acc: UsageTotals, row) => {
      acc.input += row.inputTokens ?? 0
      acc.output += row.outputTokens ?? 0
      acc.cost += row.costUsd ?? 0
      acc.cacheRead += row.cacheReadTokens ?? 0
      acc.cacheWrite += row.cacheWriteTokens ?? 0
      acc.billedCost += row.billedCostUsd ?? 0
      acc.estimatedCost += row.estimatedCostUsd ?? 0
      acc.estimatedEventCount += row.estimatedEventCount ?? 0
      acc.missingCostEntries += row.missingCostEntries ?? 0
      return acc
    }, {
      input: 0,
      output: 0,
      cost: 0,
      cacheRead: 0,
      cacheWrite: 0,
      sessions: options.visibleSessions.value.length,
      totalTokens: 0,
      billedCost: 0,
      estimatedCost: 0,
      estimatedEventCount: 0,
      missingCostEntries: 0,
      eventCount: 0,
      costSource: 'none',
      costSourceCounts: {},
    })
    totals.totalTokens = totals.input + totals.output
    return totals
  })

  const totalTokensDisplay = computed(() => {
    const t = usageTotals.value
    const total = t.input + t.output
    return total != null ? total.toLocaleString() : '-'
  })

  const tokensBreakdownParts = computed(() => {
    const t = usageTotals.value
    const parts: Array<{ label: string; value: string }> = []
    if (t.input != null) parts.push({ label: i18n.global.t('usageLogs.tokenParts.in'), value: t.input.toLocaleString() })
    if (t.output != null) parts.push({ label: i18n.global.t('usageLogs.tokenParts.out'), value: t.output.toLocaleString() })
    if (t.cacheRead) parts.push({ label: i18n.global.t('usageLogs.tokenParts.cacheRead'), value: t.cacheRead.toLocaleString() })
    if (t.cacheWrite) parts.push({ label: i18n.global.t('usageLogs.tokenParts.cacheWrite'), value: t.cacheWrite.toLocaleString() })
    return parts
  })

  const nativeDisplay = computed(() => nativeBillingDisplay(
    usageTotals.value,
    usageTotals.value.cost,
  ))

  const totalCostDisplay = computed(() => options.fmtCost(
    usageTotals.value.cost,
    {
      decimals: 4,
      source: usageTotals.value,
    },
  ))

  const sessionCountDisplay = computed(() => {
    const n = usageTotals.value.taskCount ?? usageTotals.value.sessions
    return n != null ? String(n) : '-'
  })

  const avgCostDisplay = computed(() => {
    const t = usageTotals.value
    const count = t.taskCount ?? t.sessions
    const avg = count > 0 ? t.cost / count : null
    if (avg == null) return '-'
    const native = nativeDisplay.value
    if (options.currency.value === 'CNY' && native.exactCny != null) {
      return `¥${(native.exactCny / count).toFixed(4)}`
    }
    return options.fmtCost(avg, {
      decimals: 4,
      source: t,
    })
  })

  return {
    usageTotals,
    totalTokensDisplay,
    tokensBreakdownParts,
    totalCostDisplay,
    sessionCountDisplay,
    avgCostDisplay,
  }
}
