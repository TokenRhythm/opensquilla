import type { InjectionKey } from 'vue'

export interface UsageReportingRequestOptions {
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
}

export interface UsageStatusSession {
  readonly session?: string
  readonly sessionKey?: string
  readonly key?: string
  readonly input_tokens?: number
  readonly inputTokens?: number
  readonly output_tokens?: number
  readonly outputTokens?: number
  readonly cache_read_tokens?: number
  readonly cacheReadTokens?: number
  readonly cache_write_tokens?: number
  readonly cacheWriteTokens?: number
  readonly cost_usd?: number
  readonly costUsd?: number
  readonly model?: string
  readonly contextStatus?: Readonly<Record<string, unknown>> | null
  readonly context_status?: Readonly<Record<string, unknown>> | null
  readonly [key: string]: unknown
}

export interface UsageStatusResult {
  readonly sessions?: readonly UsageStatusSession[]
  readonly totals?: Readonly<{ tokens?: number; [key: string]: unknown }>
  readonly totalTokens?: number
  readonly total_tokens?: number
  readonly [key: string]: unknown
}

export interface UsageReportQuery {
  readonly schemaVersion?: number
  readonly range?: Readonly<{ preset?: string }>
  readonly timezone?: string
  readonly include?: Readonly<{
    days?: boolean
    models?: boolean
    sessions?: boolean
  }>
}

export type UsageQueryResult = Readonly<Record<string, unknown>>
export type UsageCostBreakdown = Readonly<Record<string, unknown>>

/** Business usage reporting; Gateway method availability stays in the Adapter. */
export interface UsageReporting {
  status(
    sessionKey?: string,
    options?: UsageReportingRequestOptions,
  ): Promise<UsageStatusResult>
  query(
    query: UsageReportQuery,
    options?: UsageReportingRequestOptions,
  ): Promise<UsageQueryResult>
  costBreakdown(
    query?: UsageReportQuery,
    options?: UsageReportingRequestOptions,
  ): Promise<UsageCostBreakdown>
}

export const USAGE_REPORTING_KEY: InjectionKey<UsageReporting> = Symbol('UsageReporting')
