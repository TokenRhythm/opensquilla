import type { NativeBilledByCurrency, UsageSnapshot } from '@/types/usage'
import i18n from '@/i18n'

const NANOS_PER_USD = 1_000_000_000

type NativeBillingSource = {
  readonly nativeBilledByCurrency?: NativeBilledByCurrency
  readonly pendingBillingReceiptCount?: number
  readonly costSource?: string
  readonly costSourceCounts?: Record<string, number>
  readonly missingCostEntries?: number | null
  readonly nativeBillingExpectedReceiptCount?: number
  readonly nativeBillingMissingConfirmedReceiptCount?: number
} | undefined

export function hasUnpricedUsage(source: NativeBillingSource): boolean {
  return (source?.missingCostEntries ?? 0) > 0
    || (source?.costSourceCounts?.unavailable ?? 0) > 0
    || source?.costSource === 'unavailable'
}

export interface NativeBillingDisplay {
  exactCny: number | null
}

function nativeByCurrency(source: NativeBillingSource): NativeBilledByCurrency {
  return source?.nativeBilledByCurrency || {}
}

function nonNegativeInteger(value: number | undefined): number | null {
  if (value == null) return null
  const count = Number(value)
  return Number.isInteger(count) && count >= 0 ? count : null
}

function usdEquivalentMatches(entryNanos: string, canonicalUsd: number): boolean {
  if (!Number.isFinite(canonicalUsd)) return false
  try {
    const receiptNanos = BigInt(entryNanos)
    const canonicalNanos = BigInt(Math.round(canonicalUsd * NANOS_PER_USD))
    const delta = receiptNanos >= canonicalNanos
      ? receiptNanos - canonicalNanos
      : canonicalNanos - receiptNanos
    return delta <= 1n
  } catch {
    return false
  }
}

/**
 * Decide whether a cost can be rendered as an exact native-CNY amount.
 *
 * Matching the receipt's USD equivalent to the canonical row total prevents a
 * partially covered, pre-cutover row from being mistaken for a pure CNY row.
 */
export function nativeBillingDisplay(
  source: NativeBillingSource,
  canonicalUsd: number,
): NativeBillingDisplay {
  const native = nativeByCurrency(source)
  const entries = Object.entries(native)
    .filter(([, entry]) => Number(entry?.receiptCount || 0) > 0)
  const pendingReceiptCount = Number(source?.pendingBillingReceiptCount || 0)
  const costSource = String(source?.costSource || 'none')
  const expectedReceiptCount = nonNegativeInteger(source?.nativeBillingExpectedReceiptCount)
  const missingReceiptCount = nonNegativeInteger(
    source?.nativeBillingMissingConfirmedReceiptCount,
  )

  let exactCny: number | null = null
  if (
    costSource === 'provider_billed'
    && pendingReceiptCount === 0
    && entries.length === 1
    && entries[0][0].toUpperCase() === 'CNY'
  ) {
    const entry = entries[0][1]
    const amount = Number(entry.amount)
    const receiptCount = Number(entry.receiptCount)
    if (
      Number.isFinite(amount)
      && expectedReceiptCount != null
      && expectedReceiptCount > 0
      && missingReceiptCount === 0
      && receiptCount === expectedReceiptCount
      && usdEquivalentMatches(entry.usdEquivalentNanos, canonicalUsd)
    ) {
      exactCny = amount
    }
  }

  return { exactCny }
}

export function serializeNativeBilling(source: NativeBillingSource): string {
  const native = nativeByCurrency(source)
  return Object.keys(native).length > 0 ? JSON.stringify(native) : ''
}

function positiveRate(value: unknown): number | null {
  if (value == null || value === '') return null
  const rate = Number(value)
  return Number.isFinite(rate) && rate > 0 ? rate : null
}

/**
 * The CNY-per-USD rate the ledger actually normalized receipts with.
 *
 * Prefers the gateway-served canonical rate; when talking to an older gateway
 * that predates it, falls back to the receipt-recorded normalization rate if
 * the snapshot's receipts agree on exactly one. Returns null when neither is
 * available (legacy usage.status fallback), letting callers keep their
 * historical default rate.
 */
export function effectiveCnyPerUsd(snapshot: UsageSnapshot | null | undefined): number | null {
  if (!snapshot) return null
  const served = positiveRate(snapshot.fxRatesNativePerUsd?.CNY)
  if (served != null) return served
  const receiptRates = snapshot.totals?.nativeBilledByCurrency?.CNY?.normalizationRatesNativePerUsd
  if (Array.isArray(receiptRates)) {
    const unique = new Set<number>()
    for (const rate of receiptRates) {
      const parsed = positiveRate(rate)
      if (parsed != null) unique.add(parsed)
    }
    if (unique.size === 1) return [...unique][0]
  }
  return null
}

export function formatUsageCost(
  usd: number | null | undefined,
  currency: string,
  cnyRate: number,
  decimals = 4,
  source?: NativeBillingSource,
): string {
  if (usd == null) return '—'
  const canonicalUsd = Number(usd)
  const incomplete = hasUnpricedUsage(source)
  if (incomplete && canonicalUsd === 0) return i18n.global.t('usageLogs.costSource.unpriced.label')
  const native = nativeBillingDisplay(source, canonicalUsd)
  // Native receipt coverage determines whether CNY can be shown as an exact
  // provider-billed amount, but it must not override the user's display
  // currency. Mixed, pending, and older rows still have a canonical USD total
  // that can be converted with the snapshot's effective display rate.
  return currency === 'CNY'
    ? '¥' + (native.exactCny ?? canonicalUsd * cnyRate).toFixed(decimals)
    : '$' + canonicalUsd.toFixed(decimals)
}
