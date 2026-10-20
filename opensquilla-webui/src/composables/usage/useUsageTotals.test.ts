import { computed, ref } from 'vue'
import { describe, expect, it } from 'vitest'

import { formatUsageCost } from './nativeBilling'
import { useUsageTotals } from './useUsageTotals'
import type { UsageTotals } from '@/types/usage'

function totals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return {
    input: 1,
    output: 1,
    cost: 1,
    cacheRead: 0,
    cacheWrite: 0,
    sessions: 1,
    totalTokens: 2,
    billedCost: 1,
    estimatedCost: 0,
    estimatedEventCount: 0,
    missingCostEntries: 0,
    eventCount: 1,
    costSource: 'provider_billed',
    costSourceCounts: { provider_billed: 1 },
    ...overrides,
  }
}

function cnyReceipt() {
  return {
    CNY: {
      amountNanos: '6975000000',
      amount: '6.975',
      usdEquivalentNanos: '1000000000',
      receiptCount: 1,
      normalizationRatesNativePerUsd: ['6.975'],
    },
  }
}

function summary(source: UsageTotals, selectedCurrency: 'USD' | 'CNY') {
  const currency = ref(selectedCurrency)
  return useUsageTotals({
    visibleSessions: computed(() => []),
    serverTotals: computed(() => source),
    currency,
    fmtCost: (usd, options) => formatUsageCost(
      usd,
      currency.value,
      7.25,
      options?.decimals,
      options?.source,
    ),
  })
}

describe('usage total native billing presentation', () => {
  it('uses parent task count for count and average while retaining authoritative total cost', () => {
    const result = summary(totals({ cost: 3, sessions: 5, taskCount: 2 }), 'USD')
    expect(result.sessionCountDisplay.value).toBe('2')
    expect(result.totalCostDisplay.value).toBe('$3.0000')
    expect(result.avgCostDisplay.value).toBe('$1.5000')
    const empty = summary(totals({ cost: 3, sessions: 5, taskCount: 0 }), 'USD')
    expect(empty.sessionCountDisplay.value).toBe('0')
    expect(empty.avgCostDisplay.value).toBe('-')
    expect(empty.totalCostDisplay.value).toBe('$3.0000')
  })

  it('does not turn missing charges into a zero total or average', () => {
    const result = summary(totals({
      cost: 0, billedCost: 0, missingCostEntries: 1,
      costSource: 'unavailable', costSourceCounts: { unavailable: 1 },
    }), 'CNY')
    expect(result.totalCostDisplay.value).toBe('Unpriced')
    expect(result.avgCostDisplay.value).toBe('Unpriced')
  })

  it('reactively converts mixed billing totals when the display currency changes', () => {
    const source = totals({
      cost: 3,
      billedCost: 2,
      estimatedCost: 1,
      costSource: 'mixed',
      costSourceCounts: { provider_billed: 2, opensquilla_estimate: 1 },
      nativeBilledByCurrency: {
        ...cnyReceipt(),
        USD: {
          amountNanos: '1000000000',
          amount: '1',
          usdEquivalentNanos: '1000000000',
          receiptCount: 1,
          normalizationRatesNativePerUsd: ['1'],
        },
      },
    })
    const currency = ref('USD')
    const result = useUsageTotals({
      visibleSessions: computed(() => []),
      serverTotals: computed(() => source),
      currency,
      fmtCost: (usd, options) => formatUsageCost(
        usd,
        currency.value,
        7.25,
        options?.decimals,
        options?.source,
      ),
    })

    expect(result.totalCostDisplay.value).toBe('$3.0000')
    expect(result.avgCostDisplay.value).toBe('$3.0000')

    currency.value = 'CNY'

    expect(result.totalCostDisplay.value).toBe('¥21.7500')
    expect(result.avgCostDisplay.value).toBe('¥21.7500')
  })

  it('uses exact CNY for a pure confirmed row', () => {
    const source = totals({
      nativeBilledByCurrency: cnyReceipt(),
      nativeBillingExpectedReceiptCount: 1,
      nativeBillingMissingConfirmedReceiptCount: 0,
    })
    const cny = summary(source, 'CNY')
    const usd = summary(source, 'USD')

    expect(cny.totalCostDisplay.value).toBe('¥6.9750')
    expect(cny.avgCostDisplay.value).toBe('¥6.9750')
    expect(usd.totalCostDisplay.value).toBe('$1.0000')
    expect(usd.avgCostDisplay.value).toBe('$1.0000')
  })

  it('converts mixed native currency totals', () => {
    const source = totals({
      cost: 3,
      billedCost: 2,
      estimatedCost: 1,
      costSource: 'mixed',
      costSourceCounts: { provider_billed: 2, opensquilla_estimate: 1 },
      nativeBilledByCurrency: {
        ...cnyReceipt(),
        USD: {
          amountNanos: '1000000000',
          amount: '1',
          usdEquivalentNanos: '1000000000',
          receiptCount: 1,
          normalizationRatesNativePerUsd: ['1'],
        },
      },
    })
    const result = summary(source, 'CNY')

    expect(result.totalCostDisplay.value).toBe('¥21.7500')
    expect(result.avgCostDisplay.value).toBe('¥21.7500')
  })

  it('converts pending billing estimates', () => {
    const source = totals({
      cost: 0.5,
      billedCost: 0,
      estimatedCost: 0.5,
      estimatedEventCount: 1,
      costSource: 'opensquilla_estimate',
      costSourceCounts: { opensquilla_estimate: 1 },
      pendingBillingReceiptCount: 1,
    })
    const result = summary(source, 'CNY')

    expect(result.totalCostDisplay.value).toBe('¥3.6250')
    expect(result.avgCostDisplay.value).toBe('¥3.6250')
  })

  it('keeps legacy no-receipt estimates on the generic display rate', () => {
    const source = totals({
      costSource: 'opensquilla_estimate',
      costSourceCounts: { opensquilla_estimate: 1 },
      billedCost: 0,
      estimatedCost: 1,
    })
    const result = summary(source, 'CNY')

    expect(result.totalCostDisplay.value).toBe('¥7.2500')
    expect(result.avgCostDisplay.value).toBe('¥7.2500')
  })

  it('divides exact native CNY rather than reapplying the generic UI rate', () => {
    const source = totals({
      cost: 2,
      billedCost: 2,
      sessions: 2,
      eventCount: 2,
      costSourceCounts: { provider_billed: 2 },
      nativeBilledByCurrency: {
        CNY: {
          amountNanos: '13950000000',
          amount: '13.95',
          usdEquivalentNanos: '2000000000',
          receiptCount: 2,
          normalizationRatesNativePerUsd: ['6.975'],
        },
      },
      nativeBillingExpectedReceiptCount: 2,
      nativeBillingMissingConfirmedReceiptCount: 0,
    })
    const result = summary(source, 'CNY')

    expect(result.totalCostDisplay.value).toBe('¥13.9500')
    expect(result.avgCostDisplay.value).toBe('¥6.9750')
  })
})
