import { describe, expect, it } from 'vitest'

import {
  effectiveCnyPerUsd,
  formatUsageCost,
  nativeBillingDisplay,
  serializeNativeBilling,
} from './nativeBilling'
import type { UsageSnapshot, UsageTotals } from '@/types/usage'

function receipt(
  currency: string,
  amount: string,
  usdEquivalentNanos: string,
): Record<string, unknown> {
  return {
    costSource: 'provider_billed',
    costSourceCounts: { provider_billed: 1 },
    pendingBillingReceiptCount: 0,
    nativeBillingExpectedReceiptCount: 1,
    nativeBillingMissingConfirmedReceiptCount: 0,
    nativeBilledByCurrency: {
      [currency]: {
        amountNanos: '6975000000',
        amount,
        usdEquivalentNanos,
        receiptCount: 1,
        normalizationRatesNativePerUsd: currency === 'CNY' ? ['6.975'] : ['1'],
      },
    },
  }
}

describe('native billing display contract', () => {
  it('shows a fully covered confirmed CNY receipt as exact', () => {
    const source = receipt('CNY', '6.975', '1000000000')

    expect(nativeBillingDisplay(source, 1)).toMatchObject({
      exactCny: 6.975,
      useCanonicalUsd: false,
      subtotalText: '¥6.975',
    })
    expect(formatUsageCost(1, 'CNY', 7.25, 4, source)).toBe('¥6.9750')
    expect(formatUsageCost(1, 'USD', 7.25, 4, source)).toBe('$1.0000')
  })

  it('preserves an actual zero-cost receipt as exact', () => {
    const source = receipt('CNY', '0', '0')

    expect(nativeBillingDisplay(source, 0).exactCny).toBe(0)
    expect(formatUsageCost(0, 'CNY', 7.25, 4, source)).toBe('¥0.0000')
  })

  it('does not mislabel partially covered pre-cutover cost as exact CNY', () => {
    const source = receipt('CNY', '3.4875', '500000000')

    expect(nativeBillingDisplay(source, 1)).toMatchObject({
      exactCny: null,
      useCanonicalUsd: true,
    })
    expect(formatUsageCost(1, 'CNY', 7.25, 4, source)).toBe('$1.0000')
  })

  it('requires one confirmed CNY receipt for every billed physical request', () => {
    const source = receipt('CNY', '0', '0')
    source.nativeBillingExpectedReceiptCount = 2
    source.nativeBillingMissingConfirmedReceiptCount = 1

    expect(nativeBillingDisplay(source, 0)).toMatchObject({
      exactCny: null,
      useCanonicalUsd: true,
    })
    expect(formatUsageCost(0, 'CNY', 7.25, 4, source)).toBe('$0.0000')
  })

  it('accepts multiple physical B5 receipts for one billed envelope', () => {
    const source = receipt('CNY', '6.975', '1000000000')
    const cny = (source.nativeBilledByCurrency as Record<string, Record<string, unknown>>).CNY
    cny.receiptCount = 5
    source.nativeBillingExpectedReceiptCount = 5

    expect(nativeBillingDisplay(source, 1)).toMatchObject({
      exactCny: 6.975,
      useCanonicalUsd: false,
    })
    expect(formatUsageCost(1, 'CNY', 7.25, 4, source)).toBe('¥6.9750')
  })

  it('safely declines exact native display when an older row omits coverage counts', () => {
    const source = receipt('CNY', '6.975', '1000000000')
    delete source.nativeBillingExpectedReceiptCount
    delete source.nativeBillingMissingConfirmedReceiptCount

    expect(nativeBillingDisplay(source, 1).exactCny).toBeNull()
    expect(formatUsageCost(1, 'CNY', 7.25, 4, source)).toBe('$1.0000')
  })

  it('keeps pending and mixed-currency totals in canonical USD', () => {
    const pending = {
      costSource: 'opensquilla_estimate',
      pendingBillingReceiptCount: 1,
    }
    const mixed = {
      costSource: 'mixed',
      nativeBilledByCurrency: {
        CNY: {
          amountNanos: '6975000000',
          amount: '6.975',
          usdEquivalentNanos: '1000000000',
          receiptCount: 1,
          normalizationRatesNativePerUsd: ['6.975'],
        },
        USD: {
          amountNanos: '2000000000',
          amount: '2',
          usdEquivalentNanos: '2000000000',
          receiptCount: 1,
          normalizationRatesNativePerUsd: ['1'],
        },
      },
    }

    expect(formatUsageCost(0.5, 'CNY', 7.25, 4, pending)).toBe('$0.5000')
    expect(nativeBillingDisplay(mixed, 3)).toMatchObject({
      exactCny: null,
      useCanonicalUsd: true,
      subtotalText: '¥6.975 · $2',
    })
    expect(formatUsageCost(3, 'CNY', 7.25, 4, mixed)).toBe('$3.0000')
  })

  it('retains the legacy approximate conversion when no receipt exists', () => {
    expect(formatUsageCost(1, 'CNY', 7.25, 4, {
      costSource: 'opensquilla_estimate',
    })).toBe('¥7.2500')
  })

  it('serializes nano values as strings without losing precision', () => {
    const source = receipt('CNY', '6.975', '9007199254740993123')
    const serialized = serializeNativeBilling(source)

    expect(JSON.parse(serialized).CNY.usdEquivalentNanos).toBe('9007199254740993123')
  })
})

function snapshot(overrides: {
  fxRatesNativePerUsd?: Record<string, string>
  cnyReceiptRates?: string[]
}): UsageSnapshot {
  const totals = {
    nativeBilledByCurrency: overrides.cnyReceiptRates
      ? {
        CNY: {
          amountNanos: '6975000000',
          amount: '6.975',
          usdEquivalentNanos: '1000000000',
          receiptCount: 1,
          normalizationRatesNativePerUsd: overrides.cnyReceiptRates,
        },
      }
      : {},
  } as unknown as UsageTotals
  return {
    totals,
    ...(overrides.fxRatesNativePerUsd
      ? { fxRatesNativePerUsd: overrides.fxRatesNativePerUsd }
      : {}),
  } as UsageSnapshot
}

describe('effective CNY-per-USD rate', () => {
  it('prefers the gateway-served canonical rate', () => {
    expect(effectiveCnyPerUsd(snapshot({
      fxRatesNativePerUsd: { CNY: '6.975' },
      cnyReceiptRates: ['7.1'],
    }))).toBe(6.975)
  })

  it('falls back to a unanimous receipt-recorded rate on older gateways', () => {
    expect(effectiveCnyPerUsd(snapshot({ cnyReceiptRates: ['6.975'] }))).toBe(6.975)
  })

  it('declines to guess when receipts disagree or nothing is recorded', () => {
    expect(effectiveCnyPerUsd(snapshot({ cnyReceiptRates: ['6.975', '7.1'] }))).toBeNull()
    expect(effectiveCnyPerUsd(snapshot({}))).toBeNull()
    expect(effectiveCnyPerUsd(null)).toBeNull()
  })

  it('rejects non-positive or malformed served rates', () => {
    expect(effectiveCnyPerUsd(snapshot({
      fxRatesNativePerUsd: { CNY: 'not-a-rate' },
    }))).toBeNull()
    expect(effectiveCnyPerUsd(snapshot({
      fxRatesNativePerUsd: { CNY: '0' },
    }))).toBeNull()
  })
})
