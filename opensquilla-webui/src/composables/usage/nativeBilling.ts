import type { NativeBilledByCurrency, UsageSnapshot } from '@/types/usage'

const NANOS_PER_USD = 1_000_000_000

type NativeBillingSource = Record<string, unknown> | undefined

export interface NativeBillingDisplay {
  exactCny: number | null
  hasNativeReceipts: boolean
  useCanonicalUsd: boolean
  pendingReceiptCount: number
  subtotalText: string
}

function value(source: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (source[key] != null) return source[key]
  }
  return undefined
}

function nativeByCurrency(source: NativeBillingSource): NativeBilledByCurrency {
  if (!source) return {}
  const native = value(source, 'native_billed_by_currency', 'nativeBilledByCurrency')
  return native && typeof native === 'object' && !Array.isArray(native)
    ? native as NativeBilledByCurrency
    : {}
}

function nonNegativeInteger(
  source: NativeBillingSource,
  ...keys: string[]
): number | null {
  if (!source) return null
  const rawCount = value(source, ...keys)
  if (rawCount == null) return null
  const count = Number(rawCount)
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

function formatNativeAmount(currency: string, amount: string): string {
  if (currency === 'CNY') return `¥${amount}`
  if (currency === 'USD') return `$${amount}`
  return `${amount} ${currency}`
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
    .sort(([left], [right]) => left.localeCompare(right))
  const pendingReceiptCount = Number(
    source ? value(source, 'pending_billing_receipt_count', 'pendingBillingReceiptCount') || 0 : 0,
  )
  const costSource = String(
    source ? value(source, 'cost_source', 'costSource') || 'none' : 'none',
  )
  const expectedReceiptCount = nonNegativeInteger(
    source,
    'native_billing_expected_receipt_count',
    'nativeBillingExpectedReceiptCount',
  )
  const missingReceiptCount = nonNegativeInteger(
    source,
    'native_billing_missing_confirmed_receipt_count',
    'nativeBillingMissingConfirmedReceiptCount',
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

  const hasNativeReceipts = entries.length > 0
  const hasIncompleteCnyReceipt = entries.length === 1
    && entries[0][0].toUpperCase() === 'CNY'
    && exactCny == null
  const useCanonicalUsd = costSource === 'mixed'
    || pendingReceiptCount > 0
    || entries.length > 1
    || hasIncompleteCnyReceipt

  return {
    exactCny,
    hasNativeReceipts,
    useCanonicalUsd,
    pendingReceiptCount: Number.isFinite(pendingReceiptCount) ? pendingReceiptCount : 0,
    subtotalText: entries
      .map(([currency, entry]) => formatNativeAmount(currency.toUpperCase(), entry.amount))
      .join(' · '),
  }
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
  if (currency !== 'CNY') return '$' + canonicalUsd.toFixed(decimals)
  const native = nativeBillingDisplay(source, canonicalUsd)
  if (native.exactCny != null) return '¥' + native.exactCny.toFixed(decimals)
  if (native.useCanonicalUsd) return '$' + canonicalUsd.toFixed(decimals)
  return '¥' + (canonicalUsd * cnyRate).toFixed(decimals)
}
