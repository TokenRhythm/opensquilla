import {
  readTransportFailure,
  type TransportCallOptions,
} from './transportTypes'
import {
  USAGE_STATUS_METHOD,
  type Params as UsageStatusParams,
  type Result as UsageStatusWireResult,
} from '@/contracts/generated/v4/usageStatus'
import { validateResult as validateUsageStatusResult } from '@/contracts/generated/v4/usageStatusValidators.mjs'
import {
  USAGE_QUERY_METHOD,
  type Params as UsageQueryParams,
  type Result as UsageQueryWireResult,
} from '@/contracts/generated/v4/usageQuery'
import { validateResult as validateUsageQueryResult } from '@/contracts/generated/v4/usageQueryValidators.mjs'
import {
  USAGE_COST_METHOD,
  type Result as UsageCostWireResult,
} from '@/contracts/generated/v4/usageCost'
import { validateResult as validateUsageCostResult } from '@/contracts/generated/v4/usageCostValidators.mjs'
import type {
  UsageContextStatus,
  UsageCostBreakdown,
  UsageReporting,
  UsageReportingRequestOptions,
  UsageSnapshotRequestOptions,
  UsageStatusResult,
  UsageStatusSession,
} from '@/modules/usageReporting'
import type {
  ModelBreakdownItem,
  ModelCard,
  NativeBilledByCurrency,
  UsageCoverage,
  UsageRangeSelection,
  UsageSession,
  UsageSnapshot,
  UsageTotals,
} from '@/types/usage'

interface UsageReportingTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  ready?(options?: { signal?: AbortSignal }): Promise<void>
  supports?(method: string): boolean
  markUnsupported?(method: string): void
}

type WireRecord = Record<string, unknown>

const NANO_USD = 1_000_000_000
const MICRO_USD = 1_000_000

function callOptions(value?: UsageReportingRequestOptions): TransportCallOptions | undefined {
  if (!value) return undefined
  return {
    signal: value.signal,
    timeoutMs: value.timeoutMs,
    timeoutAction: 'reject',
    abortAction: 'reject',
  }
}

async function request<T>(
  transport: UsageReportingTransport,
  method: string,
  params: Record<string, unknown> | undefined,
  requestOptions?: UsageReportingRequestOptions,
): Promise<T> {
  const mapped = callOptions(requestOptions)
  return mapped
    ? transport.request<T>(method, params, mapped)
    : transport.request<T>(method, params)
}

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

function record(value: unknown): WireRecord | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as WireRecord
    : undefined
}

function rawValue(source: WireRecord | undefined, ...keys: string[]): unknown {
  if (!source) return undefined
  for (const key of keys) {
    if (source[key] != null) return source[key]
  }
  return undefined
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function finiteNumber(value: unknown, fallback = 0): number {
  if (value == null || value === '') return fallback
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function nullableNumber(value: unknown): number | null {
  if (value == null || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function costUsd(source: WireRecord | undefined, prefix = ''): number | null {
  for (const [camel, snake, scale] of [
    ['CostNanos', 'cost_nanos', NANO_USD],
    ['CostMicroUsd', 'cost_micro_usd', MICRO_USD],
    ['CostUsd', 'cost_usd', 1],
  ] as const) {
    const raw = rawValue(source,
      prefix ? `${prefix}${camel}` : camel[0].toLowerCase() + camel.slice(1),
      prefix ? `${prefix}_${snake}` : snake,
    )
    if (raw == null) continue
    const value = nullableNumber(raw)
    return value == null ? null : value / scale
  }
  return null
}

function normalizeNativeBilling(value: unknown): NativeBilledByCurrency {
  const source = record(value)
  if (!source) return {}
  const normalized: NativeBilledByCurrency = {}
  Object.entries(source).forEach(([currency, raw]) => {
    const entry = record(raw)
    if (!entry) return
    const rates = rawValue(
      entry,
      'normalizationRatesNativePerUsd',
      'normalization_rates_native_per_usd',
    )
    normalized[currency.toUpperCase()] = {
      amountNanos: text(rawValue(entry, 'amountNanos', 'amount_nanos') || '0'),
      amount: text(rawValue(entry, 'amount') || '0'),
      usdEquivalentNanos: text(
        rawValue(entry, 'usdEquivalentNanos', 'usd_equivalent_nanos') || '0',
      ),
      receiptCount: finiteNumber(rawValue(entry, 'receiptCount', 'receipt_count')),
      normalizationRatesNativePerUsd: Array.isArray(rates) ? rates.map(String) : [],
    }
  })
  return normalized
}

function emptyTotals(sessions = 0): UsageTotals {
  return {
    input: 0,
    output: 0,
    cost: 0,
    cacheRead: 0,
    cacheWrite: 0,
    sessions,
    totalTokens: 0,
    billedCost: 0,
    estimatedCost: 0,
    estimatedEventCount: 0,
    missingCostEntries: 0,
    eventCount: 0,
    costSource: 'none',
    costSourceCounts: {},
    nativeBilledByCurrency: {},
    pendingBillingReceiptCount: 0,
    nativeBillingExpectedReceiptCount: 0,
    nativeBillingMissingConfirmedReceiptCount: 0,
  }
}

function normalizeTotals(value: unknown, fallbackSessions = 0): UsageTotals {
  const source = record(value)
  const input = finiteNumber(rawValue(source, 'inputTokens', 'input_tokens'))
  const output = finiteNumber(rawValue(source, 'outputTokens', 'output_tokens'))
  const rawSourceCounts = record(rawValue(source, 'costSourceCounts', 'cost_source_counts'))
  const costSourceCounts: Record<string, number> = {}
  if (rawSourceCounts) {
    Object.entries(rawSourceCounts).forEach(([key, count]) => {
      costSourceCounts[key] = finiteNumber(count)
    })
  }
  return {
    input,
    output,
    cost: costUsd(source) ?? 0,
    cacheRead: finiteNumber(rawValue(source, 'cacheReadTokens', 'cache_read_tokens')),
    cacheWrite: finiteNumber(rawValue(source, 'cacheWriteTokens', 'cache_write_tokens')),
    sessions: finiteNumber(rawValue(source, 'sessionCount', 'session_count'), fallbackSessions),
    totalTokens: finiteNumber(rawValue(source, 'totalTokens', 'total_tokens'), input + output),
    billedCost: costUsd(source, 'billed') ?? 0,
    estimatedCost: costUsd(source, 'estimated') ?? 0,
    estimatedEventCount: finiteNumber(
      rawValue(source, 'estimatedEventCount', 'estimated_event_count'),
    ),
    missingCostEntries: finiteNumber(
      rawValue(source, 'missingCostEntries', 'missing_cost_entries'),
    ),
    eventCount: finiteNumber(rawValue(source, 'eventCount', 'event_count')),
    costSource: text(rawValue(source, 'costSource', 'cost_source') || 'none'),
    costSourceCounts,
    nativeBilledByCurrency: normalizeNativeBilling(
      rawValue(source, 'nativeBilledByCurrency', 'native_billed_by_currency'),
    ),
    pendingBillingReceiptCount: finiteNumber(
      rawValue(source, 'pendingBillingReceiptCount', 'pending_billing_receipt_count'),
    ),
    nativeBillingExpectedReceiptCount: finiteNumber(rawValue(
      source,
      'nativeBillingExpectedReceiptCount',
      'native_billing_expected_receipt_count',
    )),
    nativeBillingMissingConfirmedReceiptCount: finiteNumber(rawValue(
      source,
      'nativeBillingMissingConfirmedReceiptCount',
      'native_billing_missing_confirmed_receipt_count',
    )),
  }
}

function normalizeBreakdown(value: unknown): ModelBreakdownItem[] {
  if (!Array.isArray(value)) return []
  return value.map(item => {
    const source = record(item)
    const totals = normalizeTotals(record(rawValue(source, 'totals')) || source)
    return {
      model: text(rawValue(source, 'model') || 'unknown'),
      inputTokens: totals.input,
      outputTokens: totals.output,
      cacheReadTokens: totals.cacheRead,
      cacheWriteTokens: totals.cacheWrite,
      costUsd: totals.cost,
      costSource: totals.costSource,
      costSourceCounts: totals.costSourceCounts,
      nativeBilledByCurrency: totals.nativeBilledByCurrency,
      pendingBillingReceiptCount: totals.pendingBillingReceiptCount,
      nativeBillingExpectedReceiptCount: totals.nativeBillingExpectedReceiptCount,
      nativeBillingMissingConfirmedReceiptCount:
        totals.nativeBillingMissingConfirmedReceiptCount,
      costEphemeral: Boolean(rawValue(source, 'costEphemeral', 'cost_ephemeral')),
      estimateBasis: text(rawValue(source, 'estimateBasis', 'estimate_basis')),
    }
  })
}

function normalizeSession(value: unknown, legacySessionAlias = false): UsageSession {
  const source = record(value)
  const nestedTotals = record(rawValue(source, 'totals'))
  const totals = normalizeTotals(legacySessionAlias ? source : nestedTotals || source)
  const sessionId = text(rawValue(source, 'sessionId', 'session_id'))
  const sessionKey = text(rawValue(
    source,
    'sessionKey',
    'session_key',
    'key',
    ...(legacySessionAlias ? ['session'] : []),
  ))
  const session = sessionKey || sessionId || text(rawValue(source, 'session'))
  const result: UsageSession = {
    session,
    sessionKey,
    sessionId,
    inputTokens: legacySessionAlias
      ? nullableNumber(rawValue(source, 'input_tokens', 'inputTokens')) : totals.input,
    outputTokens: legacySessionAlias
      ? nullableNumber(rawValue(source, 'output_tokens', 'outputTokens')) : totals.output,
    cacheReadTokens: legacySessionAlias
      ? nullableNumber(rawValue(source, 'cache_read_tokens', 'cacheReadTokens')) : totals.cacheRead,
    cacheWriteTokens: legacySessionAlias
      ? nullableNumber(rawValue(source, 'cache_write_tokens', 'cacheWriteTokens')) : totals.cacheWrite,
    costUsd: legacySessionAlias ? costUsd(source) : totals.cost,
    billedCostUsd: legacySessionAlias ? costUsd(source, 'billed') : totals.billedCost,
    estimatedCostUsd: legacySessionAlias ? costUsd(source, 'estimated') : totals.estimatedCost,
    estimatedEventCount: legacySessionAlias
      ? nullableNumber(rawValue(source, 'estimated_event_count', 'estimatedEventCount')) : totals.estimatedEventCount,
    missingCostEntries: legacySessionAlias
      ? nullableNumber(rawValue(source, 'missing_cost_entries', 'missingCostEntries')) : totals.missingCostEntries,
    costSource: totals.costSource,
    costEphemeral: Boolean(rawValue(source, 'costEphemeral', 'cost_ephemeral')),
    estimateBasis: text(rawValue(source, 'estimateBasis', 'estimate_basis')),
    model: text(rawValue(source, 'model')),
    modelBreakdown: normalizeBreakdown(
      rawValue(source, 'modelBreakdown', 'model_breakdown'),
    ),
    costSourceCounts: totals.costSourceCounts,
    nativeBilledByCurrency: totals.nativeBilledByCurrency || {},
    pendingBillingReceiptCount: totals.pendingBillingReceiptCount || 0,
    nativeBillingExpectedReceiptCount: totals.nativeBillingExpectedReceiptCount || 0,
    nativeBillingMissingConfirmedReceiptCount:
      totals.nativeBillingMissingConfirmedReceiptCount || 0,
  }
  const optionalTextFields = [
    ['taskName', 'taskName', 'task_name'],
    ['title', 'title'],
    ['displayName', 'displayName', 'display_name'],
    ['subject', 'subject'],
    ['derivedTitle', 'derivedTitle', 'derived_title'],
  ] as const
  for (const [target, ...aliases] of optionalTextFields) {
    const field = text(rawValue(source, ...aliases))
    if (field) result[target] = field
  }
  const optionalTimeFields = [
    ['createdAt', 'createdAt', 'created_at'],
    ['updatedAt', 'lastUsageAtMs', 'last_usage_at_ms', 'updatedAt', 'updated_at'],
    ['startedAt', 'firstUsageAtMs', 'first_usage_at_ms', 'startedAt', 'started_at'],
    ['endedAt', 'endedAt', 'ended_at'],
  ] as const
  for (const [target, ...aliases] of optionalTimeFields) {
    const field = rawValue(source, ...aliases)
    if (typeof field === 'number' || typeof field === 'string') result[target] = field
  }
  return result
}

function normalizeContextStatus(value: unknown): UsageContextStatus | null {
  const source = record(value)
  if (!source) return null
  return {
    contextTokens: finiteNumber(rawValue(source, 'contextTokens', 'context_tokens')),
    contextWindowTokens: finiteNumber(
      rawValue(source, 'contextWindowTokens', 'context_window_tokens'),
    ),
    pressure: finiteNumber(rawValue(source, 'pressure')),
    warningRatio: finiteNumber(rawValue(source, 'warningRatio', 'warning_ratio'), 0.85),
  }
}

function aggregateSessions(sessions: readonly UsageSession[]): UsageTotals {
  const totals = emptyTotals(sessions.length)
  for (const session of sessions) {
    totals.input += session.inputTokens ?? 0
    totals.output += session.outputTokens ?? 0
    totals.cacheRead += session.cacheReadTokens ?? 0
    totals.cacheWrite += session.cacheWriteTokens ?? 0
    totals.cost += session.costUsd ?? 0
    totals.billedCost += session.billedCostUsd ?? 0
    totals.estimatedCost += session.estimatedCostUsd ?? 0
    totals.estimatedEventCount += session.estimatedEventCount ?? 0
    totals.missingCostEntries += session.missingCostEntries ?? 0
  }
  totals.totalTokens = totals.input + totals.output
  return totals
}

function normalizeStatus(value: unknown): UsageStatusResult {
  const source = record(value)
  const sessions: UsageStatusSession[] = Array.isArray(source?.sessions)
    ? source.sessions.map(row => {
      const session = normalizeSession(row, true)
      const wire = record(row)
      return {
        ...session,
        contextStatus: normalizeContextStatus(
          rawValue(wire, 'contextStatus', 'context_status'),
        ),
      }
    })
    : []
  const totals = aggregateSessions(sessions)
  return {
    sessions,
    totalSessions: finiteNumber(
      rawValue(source, 'totalSessions', 'total_sessions'),
      sessions.length,
    ),
    activeSessions: finiteNumber(rawValue(source, 'activeSessions', 'active_sessions')),
    totalInputTokens: finiteNumber(
      rawValue(source, 'totalInputTokens', 'total_input_tokens'),
      totals.input,
    ),
    totalOutputTokens: finiteNumber(
      rawValue(source, 'totalOutputTokens', 'total_output_tokens'),
      totals.output,
    ),
    totalTokens: finiteNumber(
      rawValue(source, 'totalTokens', 'total_tokens'),
      finiteNumber(record(source?.totals)?.tokens, totals.totalTokens),
    ),
    totalCostUsd: finiteNumber(
      rawValue(source, 'totalCostUsd', 'total_cost_usd'),
      totals.cost,
    ),
    totalCacheReadTokens: finiteNumber(
      rawValue(source, 'totalCacheReadTokens', 'total_cache_read_tokens'),
      totals.cacheRead,
    ),
    totalCacheWriteTokens: finiteNumber(
      rawValue(source, 'totalCacheWriteTokens', 'total_cache_write_tokens'),
      totals.cacheWrite,
    ),
  }
}

function normalizeModels(value: unknown): ModelCard[] {
  if (!Array.isArray(value)) return []
  const normalized = value.map(row => {
    const source = record(row)
    const totals = normalizeTotals(rawValue(source, 'totals'))
    const model = text(rawValue(source, 'model') || 'unknown')
    const provider = text(rawValue(source, 'provider') || model.split('/')[0] || '')
    return {
      model,
      provider,
      name: model.includes('/') ? model.split('/').slice(1).join('/') : model,
      inputTokens: totals.input,
      outputTokens: totals.output,
      cacheReadTokens: totals.cacheRead,
      cacheWriteTokens: totals.cacheWrite,
      costUsd: totals.cost,
      sessions: finiteNumber(
        rawValue(source, 'sessionCount', 'session_count'),
        totals.sessions,
      ),
      share: 0,
      totalTokens: totals.totalTokens,
      costSource: totals.costSource,
      costSourceCounts: totals.costSourceCounts,
      anyCacheBlind: false,
      nativeBilledByCurrency: totals.nativeBilledByCurrency,
      pendingBillingReceiptCount: totals.pendingBillingReceiptCount,
      nativeBillingExpectedReceiptCount: totals.nativeBillingExpectedReceiptCount,
      nativeBillingMissingConfirmedReceiptCount:
        totals.nativeBillingMissingConfirmedReceiptCount,
    }
  })
  const totalCost = normalized.reduce((sum, row) => sum + row.costUsd, 0)
  return normalized.map(row => ({
    ...row,
    share: totalCost > 0 ? (row.costUsd / totalCost) * 100 : 0,
  }))
}

function normalizeCoverage(value: unknown): UsageCoverage {
  const source = record(value)
  const legacy = record(rawValue(source, 'legacyUnattributed', 'legacy_unattributed'))
  const legacyTotals = record(rawValue(legacy, 'totals'))
  const reasonCodes = rawValue(source, 'reasonCodes', 'reason_codes')
  const native = record(rawValue(source, 'nativeBilling', 'native_billing'))
  const nativeReasons = rawValue(native, 'reasonCodes', 'reason_codes')
  return {
    status: text(rawValue(source, 'status') || 'complete'),
    timeAttribution: text(
      rawValue(source, 'timeAttribution', 'time_attribution') || 'complete',
    ),
    pricing: text(rawValue(source, 'pricing') || 'complete'),
    exactFromMs: nullableNumber(rawValue(source, 'exactFromMs', 'exact_from_ms')),
    backfill: text(rawValue(source, 'backfill') || 'complete'),
    reasonCodes: Array.isArray(reasonCodes) ? reasonCodes.map(String) : [],
    anomalyCount: finiteNumber(rawValue(source, 'anomalyCount', 'anomaly_count')),
    legacyIncludedInTotals: Boolean(
      rawValue(legacy, 'includedInTotals', 'included_in_totals'),
    ),
    legacyTotals: legacyTotals ? normalizeTotals(legacyTotals) : null,
    nativeBilling: {
      status: text(rawValue(native, 'status') || 'unavailable'),
      exactFromMs: nullableNumber(rawValue(native, 'exactFromMs', 'exact_from_ms')),
      reasonCodes: Array.isArray(nativeReasons) ? nativeReasons.map(String) : [],
      missingConfirmedReceiptCount: finiteNumber(rawValue(
        native,
        'missingConfirmedReceiptCount',
        'missing_confirmed_receipt_count',
      )),
      pendingReceiptCount: finiteNumber(
        rawValue(native, 'pendingReceiptCount', 'pending_receipt_count'),
      ),
    },
  }
}

function normalizeFxRates(value: unknown): Record<string, string> {
  const source = record(value)
  if (!source) return {}
  const normalized: Record<string, string> = {}
  Object.entries(source).forEach(([currency, rate]) => {
    const parsed = Number(rate)
    if (Number.isFinite(parsed) && parsed > 0) normalized[currency.toUpperCase()] = String(rate)
  })
  return normalized
}

function normalizeQuery(value: unknown): UsageSnapshot {
  const source = record(value)
  const sessions = Array.isArray(source?.sessions)
    ? source.sessions.map(row => normalizeSession(row))
    : []
  const coverage = normalizeCoverage(rawValue(source, 'coverage'))
  const range = record(rawValue(source, 'range'))
  const totals = normalizeTotals(rawValue(source, 'totals'), sessions.length)
  const days = Array.isArray(source?.days) ? source.days.map(day => {
    const row = record(day)
    return {
      date: text(rawValue(row, 'date')),
      fromMs: nullableNumber(rawValue(row, 'fromMs', 'from_ms')),
      toMs: nullableNumber(rawValue(row, 'toMs', 'to_ms')),
      totals: normalizeTotals(rawValue(row, 'totals')),
    }
  }) : []
  return {
    source: 'usage_ledger',
    mode: coverage.status === 'complete' ? 'ledger_exact' : 'ledger_partial',
    asOfMs: finiteNumber(rawValue(source, 'asOfMs', 'as_of_ms'), Date.now()),
    timezone: text(rawValue(range, 'timezone') || 'UTC'),
    timezoneFallback: null,
    range: {
      preset: text(rawValue(range, 'preset') || 'all'),
      fromMs: nullableNumber(rawValue(range, 'fromMs', 'from_ms')),
      toMs: nullableNumber(rawValue(range, 'toMs', 'to_ms')),
    },
    totals,
    sessions,
    models: normalizeModels(rawValue(source, 'models')),
    days,
    coverage,
    fxRatesNativePerUsd: normalizeFxRates(rawValue(
      source,
      'fxRatesNativePerUsd',
      'fx_rates_native_per_usd',
    )),
  }
}

function presetForRange(range: UsageRangeSelection): string {
  switch (range) {
    case 'today': return 'today'
    case '7': return 'last_7_calendar_days'
    case '14': return 'last_14_calendar_days'
    case '30': return 'last_30_calendar_days'
    default: return 'all'
  }
}

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function rangeStartMs(range: UsageRangeSelection, now: Date): number | null {
  if (range === 'all') return null
  const days = range === 'today' ? 1 : Number(range)
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  start.setDate(start.getDate() - (days - 1))
  return start.getTime()
}

function sessionTimestamp(row: UsageSession): number | null {
  for (const value of [row.endedAt, row.updatedAt, row.startedAt, row.createdAt]) {
    const timestamp = nullableNumber(value)
    if (timestamp != null) return timestamp
  }
  return null
}

function statusSnapshot(
  status: UsageStatusResult,
  range: UsageRangeSelection,
  timezone: string,
  now = new Date(),
): UsageSnapshot {
  const fromMs = rangeStartMs(range, now)
  const sessions = fromMs == null
    ? [...status.sessions]
    : status.sessions.filter(row => {
      const timestamp = sessionTimestamp(row)
      return timestamp != null && timestamp >= fromMs && timestamp <= now.getTime()
    })
  const totals = aggregateSessions(sessions)
  if (range === 'all') {
    totals.sessions = status.totalSessions
    totals.totalTokens = status.totalTokens
    totals.cost = status.totalCostUsd
    totals.input = status.totalInputTokens
    totals.output = status.totalOutputTokens
    totals.cacheRead = status.totalCacheReadTokens
    totals.cacheWrite = status.totalCacheWriteTokens
  }
  return {
    source: 'usage_status',
    mode: 'session_approximation',
    asOfMs: now.getTime(),
    timezone,
    timezoneFallback: null,
    range: {
      preset: presetForRange(range),
      fromMs,
      toMs: now.getTime(),
    },
    totals,
    sessions,
    models: [],
    days: [],
    coverage: {
      status: 'approximate',
      timeAttribution: 'session_lifetime',
      pricing: 'legacy',
      exactFromMs: null,
      backfill: 'unavailable',
      reasonCodes: ['legacy_usage_status'],
      anomalyCount: 0,
      legacyIncludedInTotals: range === 'all',
      legacyTotals: null,
      nativeBilling: {
        status: 'unavailable',
        exactFromMs: null,
        reasonCodes: ['legacy_usage_status'],
        missingConfirmedReceiptCount: 0,
        pendingReceiptCount: 0,
      },
    },
  }
}

function queryParams(
  range: UsageRangeSelection,
  timezone: string,
  options: UsageSnapshotRequestOptions,
): UsageQueryParams {
  return {
    schemaVersion: 1,
    range: { preset: presetForRange(range) },
    timezone,
    include: {
      days: options.days ?? true,
      models: options.models ?? true,
      sessions: options.sessions ?? true,
    },
  }
}

function isMissingMethod(error: unknown): boolean {
  const failure = readTransportFailure(error)
  return failure.code === 'METHOD_NOT_FOUND' || /method not found/i.test(failure.message)
}

function isInvalidTimezone(error: unknown): boolean {
  return /unknown iana timezone|invalid timezone|time zone/i.test(
    readTransportFailure(error).message,
  )
}

export function createV4UsageReporting(
  transport: UsageReportingTransport,
): UsageReporting {
  const readStatus = async (
    sessionKey?: string,
    options?: UsageReportingRequestOptions,
  ): Promise<UsageStatusResult> => {
    const params: UsageStatusParams | undefined = sessionKey ? { sessionKey } : undefined
    const raw = await request<UsageStatusWireResult>(
      transport,
      USAGE_STATUS_METHOD,
      params,
      options,
    )
    if (!validateUsageStatusResult(raw)) throw invalid(USAGE_STATUS_METHOD)
    return normalizeStatus(raw)
  }

  return {
    async snapshot(range, options = {}) {
      await transport.ready?.({ signal: options.signal })
      const timezone = options.timezone || browserTimezone()
      const requestedPreset = presetForRange(range)
      const matchingLedgerCache = options.cachedSnapshot?.source === 'usage_ledger'
        && options.cachedSnapshot.range.preset === requestedPreset
        ? options.cachedSnapshot
        : null
      let transientQueryFailure = false
      if (transport.supports?.(USAGE_QUERY_METHOD) !== false) {
        try {
          const raw = await request<UsageQueryWireResult>(
            transport,
            USAGE_QUERY_METHOD,
            queryParams(range, timezone, options),
            { ...options, timeoutMs: options.timeoutMs ?? 15_000 },
          )
          if (!validateUsageQueryResult(raw)) throw invalid(USAGE_QUERY_METHOD)
          return normalizeQuery(raw)
        } catch (error) {
          if (isMissingMethod(error)) {
            transport.markUnsupported?.(USAGE_QUERY_METHOD)
          } else if (timezone !== 'UTC' && isInvalidTimezone(error)) {
            try {
              const raw = await request<UsageQueryWireResult>(
                transport,
                USAGE_QUERY_METHOD,
                queryParams(range, 'UTC', options),
                { ...options, timeoutMs: options.timeoutMs ?? 15_000 },
              )
              if (!validateUsageQueryResult(raw)) throw invalid(USAGE_QUERY_METHOD)
              const snapshot = normalizeQuery(raw)
              return {
                ...snapshot,
                timezoneFallback: {
                  requestedTimezone: timezone,
                  effectiveTimezone: snapshot.timezone,
                  reason: 'invalid_timezone',
                },
              }
            } catch (utcError) {
              if (isMissingMethod(utcError)) transport.markUnsupported?.(USAGE_QUERY_METHOD)
              else transientQueryFailure = true
            }
          } else {
            transientQueryFailure = true
          }
        }
      }
      try {
        const status = await readStatus(undefined, {
          signal: options.signal,
          timeoutMs: options.timeoutMs ?? 15_000,
        })
        if (transientQueryFailure && matchingLedgerCache) return matchingLedgerCache
        return statusSnapshot(status, options.fallbackRange || range, timezone)
      } catch (error) {
        if (matchingLedgerCache) return matchingLedgerCache
        throw error
      }
    },
    async status(sessionKey, options) {
      await transport.ready?.({ signal: options?.signal })
      return readStatus(sessionKey, options)
    },
    async costBreakdown(options) {
      await transport.ready?.({ signal: options?.signal })
      const raw = await request<UsageCostWireResult>(
        transport,
        USAGE_COST_METHOD,
        {},
        options,
      )
      if (!validateUsageCostResult(raw)) throw invalid(USAGE_COST_METHOD)
      const source = record(raw)
      const sessions = Array.isArray(source?.breakdown)
        ? source.breakdown.map(row => normalizeSession(row, true))
        : []
      return {
        totalCostUsd: finiteNumber(rawValue(source, 'totalCostUsd', 'total_cost_usd')),
        sessions,
      } satisfies UsageCostBreakdown
    },
  }
}
