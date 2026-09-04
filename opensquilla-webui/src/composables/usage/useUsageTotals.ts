import { computed, toValue, type ComputedRef, type MaybeRefOrGetter, type Ref } from 'vue'
import i18n from '@/i18n'
import { nativeBillingDisplay } from '@/composables/usage/nativeBilling'
import type { UsageSession, UsageTotals } from '@/types/usage'

const t = i18n.global.t

export function useUsageTotals(options: {
  visibleSessions: ComputedRef<UsageSession[]>
  serverTotals?: ComputedRef<UsageTotals | null>
  currency: Ref<string>
  cnyRate: MaybeRefOrGetter<number>
  fmtCost: (
    usd: number | null | undefined,
    opts?: { decimals?: number; source?: UsageTotals },
  ) => string
  sourceCompositionHint: (rows: UsageSession[]) => string
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

  const costHintText = computed(() => {
    const visibleRows = options.visibleSessions.value
    const sourceHint = options.sourceCompositionHint(visibleRows)
    const hints: string[] = []
    const totalCostUsd = usageTotals.value.cost
    const native = nativeDisplay.value
    if (native.useCanonicalUsd) {
      if (options.currency.value === 'CNY') {
        hints.push(`≈ $${Number(totalCostUsd).toFixed(4)} USD`)
      } else {
        hints.push(`≈ ¥${(Number(totalCostUsd) * toValue(options.cnyRate)).toFixed(4)} CNY`)
      }
      if (native.subtotalText) {
        hints.push(t('usageLogs.nativeBillingSubtotals', { amounts: native.subtotalText }))
      }
      if (native.pendingReceiptCount > 0) {
        hints.push(t('usageLogs.coverage.pendingBilling', {
          count: native.pendingReceiptCount,
        }))
      }
    } else if (options.currency.value === 'CNY') {
      hints.push(`${native.exactCny == null ? '≈' : '='} ${('$' + Number(totalCostUsd).toFixed(4))} USD`)
    } else if (options.currency.value === 'USD') {
      hints.push(native.exactCny == null
        ? `≈ ¥${(Number(totalCostUsd) * toValue(options.cnyRate)).toFixed(4)} CNY`
        : `= ¥${native.exactCny.toFixed(4)} CNY`)
    }
    return [...hints, sourceHint].filter(Boolean).join(' · ')
  })

  const costHintTitle = computed(() => {
    if (nativeDisplay.value.exactCny != null) return t('usageLogs.nativeCostHintTitle')
    if (nativeDisplay.value.useCanonicalUsd && options.currency.value === 'USD') {
      return t('usageLogs.nativeMixedCostHintTitle')
    }
    return t('usageLogs.costHintTitle', { rate: toValue(options.cnyRate) })
  })

  const sessionCountDisplay = computed(() => {
    const n = usageTotals.value.sessions
    return n != null ? String(n) : '-'
  })

  const avgCostDisplay = computed(() => {
    const t = usageTotals.value
    const avg = t.sessions > 0 ? t.cost / t.sessions : null
    if (avg == null) return '-'
    const native = nativeDisplay.value
    if (options.currency.value === 'CNY' && native.exactCny != null) {
      return `¥${(native.exactCny / t.sessions).toFixed(4)}`
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
    costHintText,
    costHintTitle,
    sessionCountDisplay,
    avgCostDisplay,
  }
}
