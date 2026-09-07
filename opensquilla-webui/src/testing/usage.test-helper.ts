import type { UsageReporting, UsageStatusResult } from '@/modules/usageReporting'
import type { UsageSession, UsageSnapshot, UsageTotals } from '@/types/usage'

export function usageSession(overrides: Partial<UsageSession> = {}): UsageSession {
  return {
    session: '', sessionKey: '', sessionId: '',
    inputTokens: null, outputTokens: null, cacheReadTokens: null, cacheWriteTokens: null,
    costUsd: null, billedCostUsd: null, estimatedCostUsd: null,
    estimatedEventCount: null, missingCostEntries: null,
    costSource: 'none', costEphemeral: false, estimateBasis: '', model: '',
    modelBreakdown: [], costSourceCounts: {}, nativeBilledByCurrency: {},
    pendingBillingReceiptCount: 0, nativeBillingExpectedReceiptCount: 0,
    nativeBillingMissingConfirmedReceiptCount: 0,
    ...overrides,
  }
}

export function usageTotals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return {
    input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, sessions: 0,
    totalTokens: 0, billedCost: 0, estimatedCost: 0, estimatedEventCount: 0,
    missingCostEntries: 0, eventCount: 0, costSource: 'none', costSourceCounts: {},
    ...overrides,
  }
}

export function usageSnapshot(overrides: Partial<UsageSnapshot> = {}): UsageSnapshot {
  return {
    source: 'usage_ledger', mode: 'ledger_exact', asOfMs: 0, timezone: 'UTC',
    timezoneFallback: null, range: { preset: 'all', fromMs: null, toMs: null },
    totals: usageTotals(), sessions: [], models: [], days: [],
    coverage: {
      status: 'complete', timeAttribution: 'complete', pricing: 'complete',
      exactFromMs: null, backfill: 'complete', reasonCodes: [], anomalyCount: 0,
      legacyIncludedInTotals: false, legacyTotals: null,
      nativeBilling: {
        status: 'unavailable', exactFromMs: null, reasonCodes: [],
        missingConfirmedReceiptCount: 0, pendingReceiptCount: 0,
      },
    },
    ...overrides,
  }
}

export function usageStatus(overrides: Partial<UsageStatusResult> = {}): UsageStatusResult {
  return {
    sessions: [], totalSessions: 0, activeSessions: 0, totalInputTokens: 0,
    totalOutputTokens: 0, totalTokens: 0, totalCostUsd: 0, totalCacheReadTokens: 0,
    totalCacheWriteTokens: 0,
    ...overrides,
  }
}

export function usageReportingDouble(overrides: Partial<UsageReporting> = {}): UsageReporting {
  return {
    snapshot: async () => usageSnapshot(),
    status: async () => usageStatus(),
    costBreakdown: async () => ({ totalCostUsd: 0, sessions: [] }),
    ...overrides,
  }
}
