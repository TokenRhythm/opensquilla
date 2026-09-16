export interface ModelBreakdownItem {
  model?: string
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  costUsd?: number
  costSource?: string
  costSourceCounts?: Record<string, number>
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
  costEphemeral?: boolean
  estimateBasis?: string
}

export type UsageRangeSelection = 'all' | '7' | '14' | '30' | 'today'
export type UsageAggregationMode = 'ledger_exact' | 'ledger_partial' | 'session_approximation'

export interface NativeBilledCurrencyTotal {
  amountNanos: string
  amount: string
  usdEquivalentNanos: string
  receiptCount: number
  normalizationRatesNativePerUsd: string[]
}

export type NativeBilledByCurrency = Record<string, NativeBilledCurrencyTotal>

/** Canonical session projection published by the Usage domain. */
export interface UsageSession {
  session: string
  sessionKey: string
  sessionId: string
  taskName?: string
  title?: string
  displayName?: string
  subject?: string
  derivedTitle?: string
  createdAt?: number | string
  updatedAt?: number | string
  startedAt?: number | string
  endedAt?: number | string
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheWriteTokens: number | null
  costUsd: number | null
  billedCostUsd: number | null
  estimatedCostUsd: number | null
  estimatedEventCount: number | null
  missingCostEntries: number | null
  costSource: string
  costEphemeral: boolean
  estimateBasis: string
  model: string
  modelBreakdown: ModelBreakdownItem[]
  costSourceCounts: Record<string, number>
  nativeBilledByCurrency: NativeBilledByCurrency
  pendingBillingReceiptCount: number
  nativeBillingExpectedReceiptCount: number
  nativeBillingMissingConfirmedReceiptCount: number
}

export interface UsageDay {
  date: string
  fromMs: number | null
  toMs: number | null
  totals: UsageTotals
}

export interface UsageCoverage {
  status: string
  timeAttribution: string
  pricing: string
  exactFromMs: number | null
  backfill: string
  reasonCodes: string[]
  anomalyCount: number
  legacyIncludedInTotals: boolean
  legacyTotals: UsageTotals | null
  nativeBilling: {
    status: string
    exactFromMs: number | null
    reasonCodes: string[]
    missingConfirmedReceiptCount: number
    pendingReceiptCount: number
  }
}

export interface UsageSnapshot {
  source: 'usage_ledger' | 'usage_status'
  mode: UsageAggregationMode
  asOfMs: number
  timezone: string
  timezoneFallback: {
    requestedTimezone: string
    effectiveTimezone: string
    reason: 'invalid_timezone'
  } | null
  range: {
    preset: string
    fromMs: number | null
    toMs: number | null
  }
  totals: UsageTotals
  sessions: UsageSession[]
  models: ModelCard[]
  days: UsageDay[]
  coverage: UsageCoverage
  /**
   * Canonical native-per-USD rates from the gateway (absent when the backend
   * predates them or the snapshot came from the legacy usage.status fallback).
   */
  fxRatesNativePerUsd?: Record<string, string>
}

export interface TableColumn {
  key: string
  label: string
}

export interface ChartRow {
  sessionKey: string | null
  label: string
  inputPct: number
  outputPct: number
  totalPct: number
  valueLabel: string
}

export interface ModelCard {
  model: string
  provider: string
  name: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
  costUsd: number
  sessions: number
  share: number
  totalTokens: number
  costSource: string
  costSourceCounts?: Record<string, number>
  anyCacheBlind: boolean
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
}

export interface BreakdownRow {
  model: string
  provider: string
  name: string
  tokens: number
  cost: number
  share: number
  costSource?: string
  costSourceCounts?: Record<string, number>
  costEphemeral?: boolean
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
}

export interface UsageTotals {
  input: number
  output: number
  cost: number
  cacheRead: number
  cacheWrite: number
  sessions: number
  totalTokens: number
  billedCost: number
  estimatedCost: number
  estimatedEventCount: number
  missingCostEntries: number
  eventCount: number
  costSource: string
  costSourceCounts: Record<string, number>
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
}

export interface SortedRow {
  raw: UsageSession
  sessionKey: string
  sessionLabel: string
  rowIdentity: string
  modified: string
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheWriteTokens: number | null
  cost: number | null
  hasModelBreakdown: boolean
}
