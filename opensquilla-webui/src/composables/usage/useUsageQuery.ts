import type { RpcClientError } from '@/lib/rpc'
import type {
  ModelBreakdownItem,
  ModelCard,
  SessionRow,
  UsageCoverage,
  UsageQueryDayWire,
  UsageQueryModelWire,
  UsageQueryResponse,
  UsageQuerySessionWire,
  UsageQueryTotalsWire,
  UsageRangeSelection,
  UsageSnapshot,
  UsageStatusData,
  UsageTotals,
  NativeBilledByCurrency,
} from '@/types/usage'

const USAGE_QUERY_METHOD = 'usage.query'
const NANO_USD = 1_000_000_000
const MICRO_USD = 1_000_000

export interface UsageRpc {
  supportsMethod: (method: string) => boolean
  markMethodUnavailable: (method: string) => void
  waitForConnection: (timeoutMs?: number) => Promise<void>
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

export interface UsageQueryOptions {
  days?: boolean
  models?: boolean
  sessions?: boolean
  timezone?: string
  /** Range semantics to use only when falling back to legacy usage.status. */
  fallbackRange?: UsageRangeSelection
  /** Last ledger result for this range, retained across transient query failures. */
  cachedSnapshot?: UsageSnapshot | null
}

export function usagePresetForRange(range: UsageRangeSelection): string {
  switch (range) {
    case 'today': return 'today'
    case '7': return 'last_7_calendar_days'
    case '14': return 'last_14_calendar_days'
    case '30': return 'last_30_calendar_days'
    default: return 'all'
  }
}

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

export function naturalRangeStartMs(
  range: UsageRangeSelection,
  now: Date = new Date(),
): number | null {
  if (range === 'all') return null
  const days = range === 'today' ? 1 : Number(range)
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  start.setDate(start.getDate() - (days - 1))
  return start.getTime()
}

function rawValue(source: Record<string, unknown> | undefined, ...keys: string[]): unknown {
  if (!source) return undefined
  for (const key of keys) {
    if (source[key] != null) return source[key]
  }
  return undefined
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

function costUsd(source: Record<string, unknown> | undefined, prefix = ''): number {
  const snake = prefix ? `${prefix}_` : ''
  const nanos = rawValue(source, `${prefix ? `${prefix}CostNanos` : 'costNanos'}`, `${snake}cost_nanos`)
  if (nanos != null) return finiteNumber(nanos) / NANO_USD
  const micros = rawValue(source, `${prefix ? `${prefix}CostMicroUsd` : 'costMicroUsd'}`, `${snake}cost_micro_usd`)
  if (micros != null) return finiteNumber(micros) / MICRO_USD
  return finiteNumber(rawValue(source, `${prefix ? `${prefix}CostUsd` : 'costUsd'}`, `${snake}cost_usd`))
}

function normalizeNativeBilling(value: unknown): NativeBilledByCurrency {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const normalized: NativeBilledByCurrency = {}
  Object.entries(value as Record<string, unknown>).forEach(([currency, raw]) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return
    const record = raw as Record<string, unknown>
    const rates = rawValue(
      record,
      'normalizationRatesNativePerUsd',
      'normalization_rates_native_per_usd',
    )
    normalized[currency.toUpperCase()] = {
      amountNanos: String(rawValue(record, 'amountNanos', 'amount_nanos') || '0'),
      amount: String(rawValue(record, 'amount') || '0'),
      usdEquivalentNanos: String(
        rawValue(record, 'usdEquivalentNanos', 'usd_equivalent_nanos') || '0',
      ),
      receiptCount: finiteNumber(rawValue(record, 'receiptCount', 'receipt_count')),
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

export function normalizeUsageTotals(
  source: UsageQueryTotalsWire | undefined,
  fallbackSessions = 0,
): UsageTotals {
  const record = source as Record<string, unknown> | undefined
  const input = finiteNumber(rawValue(record, 'inputTokens', 'input_tokens'))
  const output = finiteNumber(rawValue(record, 'outputTokens', 'output_tokens'))
  const sourceCounts = rawValue(record, 'costSourceCounts', 'cost_source_counts')
  const costSourceCounts: Record<string, number> = {}
  if (sourceCounts && typeof sourceCounts === 'object' && !Array.isArray(sourceCounts)) {
    Object.entries(sourceCounts as Record<string, unknown>).forEach(([key, value]) => {
      costSourceCounts[key] = finiteNumber(value)
    })
  }
  return {
    input,
    output,
    cost: costUsd(record),
    cacheRead: finiteNumber(rawValue(record, 'cacheReadTokens', 'cache_read_tokens')),
    cacheWrite: finiteNumber(rawValue(record, 'cacheWriteTokens', 'cache_write_tokens')),
    sessions: finiteNumber(rawValue(record, 'sessionCount', 'session_count'), fallbackSessions),
    totalTokens: finiteNumber(rawValue(record, 'totalTokens', 'total_tokens'), input + output),
    billedCost: costUsd(record, 'billed'),
    estimatedCost: costUsd(record, 'estimated'),
    estimatedEventCount: finiteNumber(rawValue(record, 'estimatedEventCount', 'estimated_event_count')),
    missingCostEntries: finiteNumber(rawValue(record, 'missingCostEntries', 'missing_cost_entries')),
    eventCount: finiteNumber(rawValue(record, 'eventCount', 'event_count')),
    costSource: String(rawValue(record, 'costSource', 'cost_source') || 'none'),
    costSourceCounts,
    nativeBilledByCurrency: normalizeNativeBilling(
      rawValue(record, 'nativeBilledByCurrency', 'native_billed_by_currency'),
    ),
    pendingBillingReceiptCount: finiteNumber(
      rawValue(record, 'pendingBillingReceiptCount', 'pending_billing_receipt_count'),
    ),
    nativeBillingExpectedReceiptCount: finiteNumber(
      rawValue(
        record,
        'nativeBillingExpectedReceiptCount',
        'native_billing_expected_receipt_count',
      ),
    ),
    nativeBillingMissingConfirmedReceiptCount: finiteNumber(
      rawValue(
        record,
        'nativeBillingMissingConfirmedReceiptCount',
        'native_billing_missing_confirmed_receipt_count',
      ),
    ),
  }
}

function aggregateSessions(sessions: SessionRow[]): UsageTotals {
  const totals = emptyTotals(sessions.length)
  for (const session of sessions) {
    const row = session as Record<string, unknown>
    totals.input += finiteNumber(rawValue(row, 'input_tokens', 'inputTokens'))
    totals.output += finiteNumber(rawValue(row, 'output_tokens', 'outputTokens'))
    totals.cacheRead += finiteNumber(rawValue(row, 'cache_read_tokens', 'cacheReadTokens'))
    totals.cacheWrite += finiteNumber(rawValue(row, 'cache_write_tokens', 'cacheWriteTokens'))
    totals.cost += costUsd(row)
    totals.billedCost += costUsd(row, 'billed')
    totals.estimatedCost += costUsd(row, 'estimated')
    totals.estimatedEventCount += finiteNumber(
      rawValue(row, 'estimated_event_count', 'estimatedEventCount'),
    )
    totals.missingCostEntries += finiteNumber(rawValue(row, 'missing_cost_entries', 'missingCostEntries'))
  }
  totals.totalTokens = totals.input + totals.output
  return totals
}

function normalizeBreakdown(items: unknown): ModelBreakdownItem[] {
  if (!Array.isArray(items)) return []
  return items.map(item => {
    const record = item && typeof item === 'object' ? item as Record<string, unknown> : {}
    const nested = rawValue(record, 'totals')
    const values = nested && typeof nested === 'object' ? nested as Record<string, unknown> : record
    return {
      ...record,
      model: String(rawValue(record, 'model') || 'unknown'),
      inputTokens: finiteNumber(rawValue(values, 'inputTokens', 'input_tokens')),
      outputTokens: finiteNumber(rawValue(values, 'outputTokens', 'output_tokens')),
      cacheReadTokens: finiteNumber(rawValue(values, 'cacheReadTokens', 'cache_read_tokens')),
      cacheWriteTokens: finiteNumber(rawValue(values, 'cacheWriteTokens', 'cache_write_tokens')),
      costUsd: costUsd(values),
      costSource: String(rawValue(values, 'costSource', 'cost_source') || 'none'),
      costSourceCounts: normalizeUsageTotals(values as UsageQueryTotalsWire).costSourceCounts,
      nativeBilledByCurrency: normalizeNativeBilling(
        rawValue(values, 'nativeBilledByCurrency', 'native_billed_by_currency'),
      ),
      pendingBillingReceiptCount: finiteNumber(
        rawValue(values, 'pendingBillingReceiptCount', 'pending_billing_receipt_count'),
      ),
      nativeBillingExpectedReceiptCount: finiteNumber(
        rawValue(
          values,
          'nativeBillingExpectedReceiptCount',
          'native_billing_expected_receipt_count',
        ),
      ),
      nativeBillingMissingConfirmedReceiptCount: finiteNumber(
        rawValue(
          values,
          'nativeBillingMissingConfirmedReceiptCount',
          'native_billing_missing_confirmed_receipt_count',
        ),
      ),
    }
  })
}

function normalizeQuerySession(row: UsageQuerySessionWire): SessionRow {
  const record = row as Record<string, unknown>
  const totals = normalizeUsageTotals(row.totals)
  const sessionKey = String(rawValue(record, 'sessionKey', 'session_key') || '')
  const sessionId = String(rawValue(record, 'sessionId', 'session_id') || '')
  const sessionLabel = sessionKey || sessionId
  const lastUsageAt = nullableNumber(rawValue(record, 'lastUsageAtMs', 'last_usage_at_ms'))
  const firstUsageAt = nullableNumber(rawValue(record, 'firstUsageAtMs', 'first_usage_at_ms'))
  return {
    ...row,
    session: sessionLabel,
    sessionKey,
    sessionId,
    ...(lastUsageAt != null ? { updatedAt: lastUsageAt } : {}),
    ...(firstUsageAt != null ? { startedAt: firstUsageAt } : {}),
    inputTokens: totals.input,
    outputTokens: totals.output,
    cacheReadTokens: totals.cacheRead,
    cacheWriteTokens: totals.cacheWrite,
    costUsd: totals.cost,
    billedCostUsd: totals.billedCost,
    estimatedCostUsd: totals.estimatedCost,
    estimatedEventCount: totals.estimatedEventCount,
    missingCostEntries: totals.missingCostEntries,
    costSource: totals.costSource,
    costSourceCounts: totals.costSourceCounts,
    nativeBilledByCurrency: totals.nativeBilledByCurrency,
    pendingBillingReceiptCount: totals.pendingBillingReceiptCount,
    nativeBillingExpectedReceiptCount: totals.nativeBillingExpectedReceiptCount,
    nativeBillingMissingConfirmedReceiptCount:
      totals.nativeBillingMissingConfirmedReceiptCount,
    modelBreakdown: normalizeBreakdown(rawValue(record, 'modelBreakdown', 'model_breakdown')),
  }
}

function normalizeQueryModels(rows: UsageQueryModelWire[]): ModelCard[] {
  const normalized = rows.map(row => {
    const totals = normalizeUsageTotals(row.totals)
    const model = String(row.model || 'unknown')
    const provider = String(row.provider || model.split('/')[0] || '')
    const name = model.includes('/') ? model.split('/').slice(1).join('/') : model
    return {
      model,
      provider,
      name,
      inputTokens: totals.input,
      outputTokens: totals.output,
      cacheReadTokens: totals.cacheRead,
      cacheWriteTokens: totals.cacheWrite,
      costUsd: totals.cost,
      sessions: finiteNumber(rawValue(row, 'sessionCount', 'session_count'), totals.sessions),
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

function normalizeDays(rows: UsageQueryDayWire[]) {
  return rows.map(row => ({
    date: String(row.date || ''),
    fromMs: nullableNumber(rawValue(row, 'fromMs', 'from_ms')),
    toMs: nullableNumber(rawValue(row, 'toMs', 'to_ms')),
    totals: normalizeUsageTotals(row.totals),
  }))
}

function normalizeCoverage(wire: UsageQueryResponse['coverage']): UsageCoverage {
  const record = wire as Record<string, unknown> | undefined
  const legacy = rawValue(record, 'legacyUnattributed', 'legacy_unattributed')
  const legacyRecord = legacy && typeof legacy === 'object'
    ? legacy as Record<string, unknown>
    : undefined
  const legacyTotals = rawValue(legacyRecord, 'totals')
  const reasons = rawValue(record, 'reasonCodes', 'reason_codes')
  const native = rawValue(record, 'nativeBilling', 'native_billing')
  const nativeRecord = native && typeof native === 'object'
    ? native as Record<string, unknown>
    : undefined
  const nativeReasons = rawValue(nativeRecord, 'reasonCodes', 'reason_codes')
  return {
    status: String(rawValue(record, 'status') || 'complete'),
    timeAttribution: String(rawValue(record, 'timeAttribution', 'time_attribution') || 'complete'),
    pricing: String(rawValue(record, 'pricing') || 'complete'),
    exactFromMs: nullableNumber(rawValue(record, 'exactFromMs', 'exact_from_ms')),
    backfill: String(rawValue(record, 'backfill') || 'complete'),
    reasonCodes: Array.isArray(reasons) ? reasons.map(String) : [],
    anomalyCount: finiteNumber(rawValue(record, 'anomalyCount', 'anomaly_count')),
    legacyIncludedInTotals: Boolean(rawValue(legacyRecord, 'includedInTotals', 'included_in_totals')),
    legacyTotals: legacyTotals && typeof legacyTotals === 'object'
      ? normalizeUsageTotals(legacyTotals as UsageQueryTotalsWire)
      : null,
    nativeBilling: {
      status: String(rawValue(nativeRecord, 'status') || 'unavailable'),
      exactFromMs: nullableNumber(rawValue(nativeRecord, 'exactFromMs', 'exact_from_ms')),
      reasonCodes: Array.isArray(nativeReasons) ? nativeReasons.map(String) : [],
      missingConfirmedReceiptCount: finiteNumber(
        rawValue(
          nativeRecord,
          'missingConfirmedReceiptCount',
          'missing_confirmed_receipt_count',
        ),
      ),
      pendingReceiptCount: finiteNumber(
        rawValue(nativeRecord, 'pendingReceiptCount', 'pending_receipt_count'),
      ),
    },
  }
}

function normalizeFxRates(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const normalized: Record<string, string> = {}
  Object.entries(value as Record<string, unknown>).forEach(([currency, rate]) => {
    if (rate == null) return
    const parsed = Number(rate)
    if (!Number.isFinite(parsed) || parsed <= 0) return
    normalized[currency.toUpperCase()] = String(rate)
  })
  return normalized
}

export function normalizeUsageQueryResponse(response: UsageQueryResponse): UsageSnapshot {
  const sessions = Array.isArray(response.sessions)
    ? response.sessions.map(normalizeQuerySession)
    : []
  const coverage = normalizeCoverage(response.coverage)
  const range = response.range || {}
  const totals = normalizeUsageTotals(response.totals, sessions.length)
  return {
    source: 'usage_ledger',
    mode: coverage.status === 'complete' ? 'ledger_exact' : 'ledger_partial',
    asOfMs: finiteNumber(response.asOfMs, Date.now()),
    timezone: String(range.timezone || 'UTC'),
    timezoneFallback: null,
    range: {
      preset: String(range.preset || 'all'),
      fromMs: nullableNumber(range.fromMs),
      toMs: nullableNumber(range.toMs),
    },
    totals,
    sessions,
    models: Array.isArray(response.models) ? normalizeQueryModels(response.models) : [],
    days: Array.isArray(response.days) ? normalizeDays(response.days) : [],
    coverage,
    fxRatesNativePerUsd: normalizeFxRates(
      rawValue(
        response as Record<string, unknown>,
        'fxRatesNativePerUsd',
        'fx_rates_native_per_usd',
      ),
    ),
  }
}

export function normalizeUsageStatusResponse(
  response: UsageStatusData,
  range: UsageRangeSelection,
  timezone = browserTimeZone(),
  now: Date = new Date(),
): UsageSnapshot {
  const allSessions = Array.isArray(response.sessions) ? response.sessions : []
  const fromMs = naturalRangeStartMs(range, now)
  const sessions = fromMs == null
    ? allSessions
    : allSessions.filter(row => {
      const timestamp = sessionTimestamp(row)
      return timestamp != null && timestamp >= fromMs && timestamp <= now.getTime()
    })
  const totals = aggregateSessions(sessions)
  if (range === 'all') {
    totals.sessions = finiteNumber(response.totalSessions, totals.sessions)
    totals.totalTokens = finiteNumber(response.totalTokens, totals.totalTokens)
    totals.cost = finiteNumber(response.totalCostUsd, totals.cost)
    totals.input = finiteNumber(response.totalInputTokens, totals.input)
    totals.output = finiteNumber(response.totalOutputTokens, totals.output)
    totals.cacheRead = finiteNumber(response.totalCacheReadTokens, totals.cacheRead)
    totals.cacheWrite = finiteNumber(response.totalCacheWriteTokens, totals.cacheWrite)
  }
  return {
    source: 'usage_status',
    mode: 'session_approximation',
    asOfMs: now.getTime(),
    timezone,
    timezoneFallback: null,
    range: {
      preset: usagePresetForRange(range),
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

function sessionTimestamp(row: SessionRow): number | null {
  const record = row as Record<string, unknown>
  for (const key of [
    'endedAt', 'ended_at', 'updatedAt', 'updated_at', 'startedAt', 'started_at',
    'createdAt', 'created_at',
  ]) {
    const value = nullableNumber(record[key])
    if (value != null) return value
  }
  return null
}

function isMethodNotFound(error: unknown): boolean {
  const rpcError = error as RpcClientError | undefined
  return rpcError?.code === 'METHOD_NOT_FOUND'
    || /method not found/i.test(error instanceof Error ? error.message : String(error))
}

function isInvalidTimezone(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /unknown iana timezone|invalid timezone|time zone/i.test(message)
}

function queryParams(
  range: UsageRangeSelection,
  timezone: string,
  options: UsageQueryOptions,
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    range: { preset: usagePresetForRange(range) },
    timezone,
    include: {
      days: options.days ?? true,
      models: options.models ?? true,
      sessions: options.sessions ?? true,
    },
  }
}

export async function requestUsageSnapshot(
  rpc: UsageRpc,
  range: UsageRangeSelection,
  options: UsageQueryOptions = {},
): Promise<UsageSnapshot> {
  await rpc.waitForConnection()
  const timezone = options.timezone || browserTimeZone()
  const requestedPreset = usagePresetForRange(range)
  const cachedSnapshot = options.cachedSnapshot
  const matchingLedgerCache = cachedSnapshot?.source === 'usage_ledger'
    && cachedSnapshot.range.preset === requestedPreset
    ? cachedSnapshot
    : null
  let transientQueryFailure = false
  if (rpc.supportsMethod(USAGE_QUERY_METHOD)) {
    try {
      const response = await rpc.call<UsageQueryResponse>(
        USAGE_QUERY_METHOD,
        queryParams(range, timezone, options),
      )
      return normalizeUsageQueryResponse(response)
    } catch (error) {
      if (isMethodNotFound(error)) {
        rpc.markMethodUnavailable(USAGE_QUERY_METHOD)
      } else if (timezone !== 'UTC' && isInvalidTimezone(error)) {
        try {
          const response = await rpc.call<UsageQueryResponse>(
            USAGE_QUERY_METHOD,
            queryParams(range, 'UTC', options),
          )
          const snapshot = normalizeUsageQueryResponse(response)
          return {
            ...snapshot,
            timezoneFallback: {
              requestedTimezone: timezone,
              effectiveTimezone: snapshot.timezone,
              reason: 'invalid_timezone',
            },
          }
        } catch (utcError) {
          if (isMethodNotFound(utcError)) {
            rpc.markMethodUnavailable(USAGE_QUERY_METHOD)
          } else {
            transientQueryFailure = true
          }
        }
      } else {
        transientQueryFailure = true
      }
      // Mixed-version upgrades must keep rendering. Any query failure falls
      // back to the legacy endpoint; if that also fails, its error is surfaced.
    }
  }
  try {
    const status = await rpc.call<UsageStatusData>('usage.status')
    // A previous ledger result is more trustworthy than a fresh session-lifetime
    // approximation. Keep it for transient query failures, but still accept the
    // legacy endpoint when the capability genuinely disappeared.
    if (transientQueryFailure && matchingLedgerCache) return matchingLedgerCache
    return normalizeUsageStatusResponse(status, options.fallbackRange || range, timezone)
  } catch (error) {
    // Background refresh failures should not erase a successful, same-range
    // ledger result. With no suitable cache, preserve the original error path.
    if (matchingLedgerCache) return matchingLedgerCache
    throw error
  }
}
