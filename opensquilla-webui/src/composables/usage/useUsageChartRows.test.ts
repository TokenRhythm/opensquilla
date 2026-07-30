import { computed, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import { formatUsageCost } from './nativeBilling'
import { useUsageChartRows } from './useUsageChartRows'
import type { UsageTotals } from '@/types/usage'

function totals(input: number, output: number, cost: number): UsageTotals {
  return {
    input,
    output,
    cost,
    cacheRead: 0,
    cacheWrite: 0,
    sessions: 1,
    totalTokens: input + output,
    billedCost: cost,
    estimatedCost: 0,
    estimatedEventCount: 0,
    missingCostEntries: 0,
    eventCount: 1,
    costSource: 'provider_billed',
    costSourceCounts: { provider_billed: 1 },
  }
}

describe('usage ledger day chart', () => {
  it('shows server calendar-day buckets newest first without turning them into session links', () => {
    const chartMode = ref<'tokens' | 'cost'>('tokens')
    const { chartCaption, chartRows } = useUsageChartRows({
      visibleSessions: computed(() => [{ sessionKey: 'should-not-drive-chart', inputTokens: 999 }]),
      serverDays: computed(() => [
        { date: '2026-07-19', fromMs: 1, toMs: 2, totals: totals(10, 5, 0.1) },
        { date: '2026-07-20', fromMs: 2, toMs: 3, totals: totals(20, 10, 0.2) },
      ]),
      chartMode,
      rowVal: (row, ...keys) => keys.map(key => row[key]).find(value => value != null),
      fmtCost: value => `$${Number(value || 0).toFixed(2)}`,
      fmtNum: value => String(value || 0),
      taskName: row => String(row.title || 'Untitled task'),
    })

    expect(chartCaption.value).toBe('Daily usage')
    expect(chartRows.value.map(row => row.label)).toEqual(['2026-07-20', '2026-07-19'])
    expect(chartRows.value.every(row => row.sessionKey === null)).toBe(true)
    expect(chartRows.value[0].valueLabel).toBe('30')
    expect(chartRows.value[0].totalPct).toBeCloseTo(100)
  })

  it('uses native billing context for exact CNY daily costs', () => {
    const chartMode = ref<'tokens' | 'cost'>('cost')
    const day = totals(10, 5, 1)
    day.nativeBilledByCurrency = {
      CNY: {
        amountNanos: '6975000000',
        amount: '6.975',
        usdEquivalentNanos: '1000000000',
        receiptCount: 1,
        normalizationRatesNativePerUsd: ['6.975'],
      },
    }
    day.nativeBillingExpectedReceiptCount = 1
    day.nativeBillingMissingConfirmedReceiptCount = 0
    const { chartRows } = useUsageChartRows({
      visibleSessions: computed(() => []),
      serverDays: computed(() => [
        { date: '2026-07-20', fromMs: 1, toMs: 2, totals: day },
      ]),
      chartMode,
      rowVal: (row, ...keys) => keys.map(key => row[key]).find(value => value != null),
      fmtCost: (value, options) => formatUsageCost(
        value,
        'CNY',
        7.25,
        4,
        options?.source as Record<string, unknown> | undefined,
      ),
      fmtNum: value => String(value || 0),
      taskName: row => String(row.title || 'Untitled task'),
    })

    expect(chartRows.value[0].valueLabel).toBe('¥6.9750')
  })

  it('uses the shared task name without shortening it in data', () => {
    const chartMode = ref<'tokens' | 'cost'>('tokens')
    const title = 'A task name long enough to require visual truncation in the chart label'
    const { chartRows } = useUsageChartRows({
      visibleSessions: computed(() => [{
        sessionKey: 'agent:main:webchat:private-id',
        title,
        inputTokens: 10,
        outputTokens: 2,
      }]),
      serverDays: computed(() => null),
      chartMode,
      rowVal: (row, ...keys) => keys.map(key => row[key]).find(value => value != null),
      fmtCost: value => `$${Number(value || 0).toFixed(2)}`,
      fmtNum: value => String(value || 0),
      taskName: row => String(row.title || 'Untitled task'),
    })

    expect(chartRows.value[0].label).toBe(title)
    expect(chartRows.value[0].sessionKey).toBe('agent:main:webchat:private-id')
  })
})
