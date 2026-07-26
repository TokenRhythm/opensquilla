export interface SessionRow {
  session?: string
  sessionKey?: string
  key?: string
  updated_at?: number | string
  updatedAt?: number | string
  endedAt?: number | string
  ended_at?: number | string
  startedAt?: number | string
  started_at?: number | string
  createdAt?: number | string
  created_at?: number | string
  input_tokens?: number | string
  inputTokens?: number | string
  output_tokens?: number | string
  outputTokens?: number | string
  cache_read_tokens?: number | string
  cacheReadTokens?: number | string
  cache_write_tokens?: number | string
  cacheWriteTokens?: number | string
  cost_usd?: number | string
  costUsd?: number | string
  cost_source?: string
  costSource?: string
  cost_ephemeral?: boolean
  costEphemeral?: boolean
  model?: string
  modelBreakdown?: ModelBreakdownItem[]
  costSourceCounts?: Record<string, number>
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
  [key: string]: unknown
}

export interface ModelBreakdownItem {
  model?: string
  inputTokens?: number | string
  input_tokens?: number | string
  outputTokens?: number | string
  output_tokens?: number | string
  costUsd?: number | string
  cost_usd?: number | string
  costSource?: string
  cost_source?: string
  costSourceCounts?: Record<string, number>
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
  costEphemeral?: boolean
  cost_ephemeral?: boolean
  [key: string]: unknown
}

export interface UsageStatusData {
  sessions?: SessionRow[]
  totalSessions?: number
  totalTokens?: number
  totalCostUsd?: number
  totalInputTokens?: number
  totalOutputTokens?: number
  totalCacheReadTokens?: number
  totalCacheWriteTokens?: number
}

export type UsageRangeSelection = 'all' | '7' | '14' | '30' | 'today'
export type UsageAggregationMode = 'ledger_exact' | 'ledger_partial' | 'session_approximation'

export interface UsageQueryTotalsWire extends Record<string, unknown> {
  inputTokens?: number | string
  input_tokens?: number | string
  outputTokens?: number | string
  output_tokens?: number | string
  cacheReadTokens?: number | string
  cache_read_tokens?: number | string
  cacheWriteTokens?: number | string
  cache_write_tokens?: number | string
  totalTokens?: number | string
  total_tokens?: number | string
  costNanos?: number | string
  cost_nanos?: number | string
  costMicroUsd?: number | string
  cost_micro_usd?: number | string
  costUsd?: number | string
  cost_usd?: number | string
  billedCostNanos?: number | string
  billed_cost_nanos?: number | string
  billedCostUsd?: number | string
  billed_cost_usd?: number | string
  estimatedCostNanos?: number | string
  estimated_cost_nanos?: number | string
  estimatedCostUsd?: number | string
  estimated_cost_usd?: number | string
  estimatedEventCount?: number | string
  estimated_event_count?: number | string
  missingCostEntries?: number | string
  missing_cost_entries?: number | string
  sessionCount?: number | string
  session_count?: number | string
  eventCount?: number | string
  event_count?: number | string
  costSource?: string
  cost_source?: string
  costSourceCounts?: Record<string, number | string>
  nativeBilledByCurrency?: Record<string, NativeBilledCurrencyWire>
  pendingBillingReceiptCount?: number | string
  nativeBillingExpectedReceiptCount?: number | string
  nativeBillingMissingConfirmedReceiptCount?: number | string
}

export interface NativeBilledCurrencyWire extends Record<string, unknown> {
  amountNanos?: string
  amount?: string
  usdEquivalentNanos?: string
  receiptCount?: number | string
  normalizationRatesNativePerUsd?: string[]
}

export interface NativeBilledCurrencyTotal {
  amountNanos: string
  amount: string
  usdEquivalentNanos: string
  receiptCount: number
  normalizationRatesNativePerUsd: string[]
}

export type NativeBilledByCurrency = Record<string, NativeBilledCurrencyTotal>

export interface UsageQuerySessionWire extends Record<string, unknown> {
  sessionId?: string
  session_id?: string
  sessionKey?: string | null
  session_key?: string | null
  firstUsageAtMs?: number | string | null
  first_usage_at_ms?: number | string | null
  lastUsageAtMs?: number | string | null
  last_usage_at_ms?: number | string | null
  totals?: UsageQueryTotalsWire
  modelBreakdown?: Array<Record<string, unknown>>
  model_breakdown?: Array<Record<string, unknown>>
}

export interface UsageQueryModelWire extends Record<string, unknown> {
  provider?: string
  model?: string
  totals?: UsageQueryTotalsWire
  eventCount?: number | string
  event_count?: number | string
  sessionCount?: number | string
  session_count?: number | string
}

export interface UsageQueryDayWire extends Record<string, unknown> {
  date?: string
  fromMs?: number | string
  from_ms?: number | string
  toMs?: number | string
  to_ms?: number | string
  totals?: UsageQueryTotalsWire
}

export interface UsageCoverageWire extends Record<string, unknown> {
  status?: string
  timeAttribution?: string
  time_attribution?: string
  pricing?: string
  exactFromMs?: number | string | null
  exact_from_ms?: number | string | null
  backfill?: string
  reasonCodes?: string[]
  reason_codes?: string[]
  anomalyCount?: number | string
  anomaly_count?: number | string
  legacyUnattributed?: {
    includedInTotals?: boolean
    included_in_totals?: boolean
    totals?: UsageQueryTotalsWire
    [key: string]: unknown
  }
  nativeBilling?: {
    status?: string
    exactFromMs?: number | string | null
    reasonCodes?: string[]
    missingConfirmedReceiptCount?: number | string
    pendingReceiptCount?: number | string
    [key: string]: unknown
  }
}

export interface UsageQueryResponse extends Record<string, unknown> {
  schemaVersion?: number
  source?: string
  asOfMs?: number | string
  /** Canonical native-per-USD display rates served by newer gateways. */
  fxRatesNativePerUsd?: Record<string, string>
  fx_rates_native_per_usd?: Record<string, string>
  range?: {
    preset?: string | null
    timezone?: string
    fromMs?: number | string | null
    toMs?: number | string | null
    endExclusive?: boolean
    [key: string]: unknown
  }
  totals?: UsageQueryTotalsWire
  attributedTotals?: UsageQueryTotalsWire
  days?: UsageQueryDayWire[]
  models?: UsageQueryModelWire[]
  sessions?: UsageQuerySessionWire[]
  coverage?: UsageCoverageWire
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
  sessions: SessionRow[]
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
  cost_source?: string
  costSourceCounts?: Record<string, number>
  costEphemeral?: boolean
  cost_ephemeral?: boolean
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
  raw: SessionRow
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
