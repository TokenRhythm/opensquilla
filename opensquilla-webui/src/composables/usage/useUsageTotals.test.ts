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
    cnyRate: 7.25,
    rowVal: (row, ...keys) => keys.map(key => row[key]).find(item => item != null),
    fmtCost: (usd, options) => formatUsageCost(
      usd,
      currency.value,
      7.25,
      options?.decimals,
      options?.source as Record<string, unknown> | undefined,
    ),
    sourceCompositionHint: () => '',
  })
}

describe('usage total native billing presentation', () => {
  it('uses exact CNY and the receipt conversion hint for a pure confirmed row', () => {
    const source = totals({
      nativeBilledByCurrency: cnyReceipt(),
      nativeBillingExpectedReceiptCount: 1,
      nativeBillingMissingConfirmedReceiptCount: 0,
    })
    const cny = summary(source, 'CNY')
    const usd = summary(source, 'USD')

    expect(cny.totalCostDisplay.value).toBe('¥6.9750')
    expect(cny.avgCostDisplay.value).toBe('¥6.9750')
    expect(cny.costHintText.value).toBe('= $1.0000 USD')
    expect(usd.totalCostDisplay.value).toBe('$1.0000')
    expect(usd.avgCostDisplay.value).toBe('$1.0000')
    expect(usd.costHintText.value).toBe('= ¥6.9750 CNY')
    expect(usd.costHintTitle.value).toContain('fixed normalization rate')
  })

  it('keeps mixed native currency canonical and lists native subtotals', () => {
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

    expect(result.totalCostDisplay.value).toBe('$3.0000')
    expect(result.avgCostDisplay.value).toBe('$3.0000')
    expect(result.costHintText.value).toContain('Original receipts: ¥6.975 · $1')
    expect(result.costHintTitle.value).toContain('canonical USD')
  })

  it('keeps pending billing canonical and surfaces the pending count', () => {
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

    expect(result.totalCostDisplay.value).toBe('$0.5000')
    expect(result.avgCostDisplay.value).toBe('$0.5000')
    expect(result.costHintText.value).toContain('1 provider billing receipts are pending')
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
    expect(result.costHintText.value).toBe('≈ $1.0000 USD')
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
