export interface ModelBreakdownItem {
  model?: string
  provider?: string
  capability?: string
  meters?: UsageMeterSummary[]
  tokenUnknownItemCount?: number
  missingCostEntries?: number
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
  usdEquivalentExact?: string
  normalizationSources?: Array<{ source: string; revision: string; rate: string }>
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
  taskGroupId?: string
  sessionEpoch?: number
  parentSessionId?: string | null
  parentSessionEpoch?: number | null
  sessionKind?: string
  attributionStatus?: string
  createdAt?: number | string
  updatedAt?: number | string
  startedAt?: number | string
  endedAt?: number | string
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheWriteTokens: number | null
  costUsd: number | null
  costUsdExact?: string
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
  attribution?: { status: string; missingEventCount: number; reasonCodes: string[] }
  measurements?: { tokenUnknownItemCount: number; unknownMeterCount: number }
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
  /** Alternative projections of the same calls; never add them to totals. */
  taskGroups?: UsageTaskGroup[]
  capabilities?: UsageCapability[]
  meters?: UsageMeterSummary[]
  settlementSummary?: UsageSettlementSummary
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

export interface UsageMeterSummary {
  metric: string
  unit: string
  dimensions: Record<string, string>
  quantity: string
  unknownCount: number
  measurementCount: number
}

export interface UsageSettlementSummary {
  provider_confirmed: number
  provider_estimate: number
  local_estimate: number
  unknown: number
}

export type UsageRunRelation = 'self' | 'inline_subagent' | 'subsession' | 'sidechat' | 'unattributed'
export type UsageBranch = 'self' | 'inlineSubagents' | 'subsessions' | 'sideChats' | 'unattributed'

export interface UsageTaskRun {
  runId: string
  sessionId: string
  sessionEpoch: number
  parentRunId: string | null
  relation: UsageRunRelation
  title: string
  totals: UsageTotals
}

export interface UsageTaskGroup {
  taskGroupId: string
  rootSessionId: string
  rootSessionEpoch: number
  sessionKey: string | null
  title: string
  firstUsageAtMs?: number | null
  lastUsageAtMs?: number | null
  modelBreakdown?: ModelBreakdownItem[]
  attributionStatus: string
  totals: UsageTotals
  branches: Record<UsageBranch, UsageTotals>
  runs: UsageTaskRun[]
}

export interface UsageCapability {
  capability: string
  totals: UsageTotals
  meters: UsageMeterSummary[]
  tokenUnknownItemCount: number
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
  capability?: string
  meters?: UsageMeterSummary[]
  tokenUnknownItemCount?: number
  missingCostEntries?: number
  costSource?: string
  costSourceCounts?: Record<string, number>
  costEphemeral?: boolean
  nativeBilledByCurrency?: NativeBilledByCurrency
  pendingBillingReceiptCount?: number
  nativeBillingExpectedReceiptCount?: number
  nativeBillingMissingConfirmedReceiptCount?: number
}

export interface UsageTotals {
  taskCount?: number
  costUsdExact?: string
  billedCostUsdExact?: string
  estimatedCostUsdExact?: string
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
