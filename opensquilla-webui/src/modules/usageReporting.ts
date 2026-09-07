import type { InjectionKey } from 'vue'
import type {
  UsageRangeSelection,
  UsageSession,
  UsageSnapshot,
} from '@/types/usage'

export interface UsageReportingRequestOptions {
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
}

export interface UsageSnapshotRequestOptions extends UsageReportingRequestOptions {
  readonly days?: boolean
  readonly models?: boolean
  readonly sessions?: boolean
  readonly timezone?: string
  readonly fallbackRange?: UsageRangeSelection
  readonly cachedSnapshot?: UsageSnapshot | null
}

export interface UsageContextStatus {
  readonly contextTokens: number
  readonly contextWindowTokens: number
  readonly pressure: number
  readonly warningRatio: number
}

export interface UsageStatusSession extends UsageSession {
  readonly contextStatus: UsageContextStatus | null
}

export interface UsageStatusResult {
  readonly sessions: readonly UsageStatusSession[]
  readonly totalSessions: number
  readonly activeSessions: number
  readonly totalInputTokens: number
  readonly totalOutputTokens: number
  readonly totalTokens: number
  readonly totalCostUsd: number
  readonly totalCacheReadTokens: number
  readonly totalCacheWriteTokens: number
}

export interface UsageCostBreakdown {
  readonly totalCostUsd: number
  readonly sessions: readonly UsageSession[]
}

/** Business usage reporting; wire availability and compatibility stay in the Adapter. */
export interface UsageReporting {
  snapshot(
    range: UsageRangeSelection,
    options?: UsageSnapshotRequestOptions,
  ): Promise<UsageSnapshot>
  status(
    sessionKey?: string,
    options?: UsageReportingRequestOptions,
  ): Promise<UsageStatusResult>
  costBreakdown(options?: UsageReportingRequestOptions): Promise<UsageCostBreakdown>
}

export const USAGE_REPORTING_KEY: InjectionKey<UsageReporting> = Symbol('UsageReporting')
