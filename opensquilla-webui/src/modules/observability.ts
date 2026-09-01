import type { InjectionKey } from 'vue'
import type {
  UsageRangeSelection,
  UsageSnapshot,
} from '@/types/usage'

export interface UsageSnapshotOptions {
  readonly days?: boolean
  readonly models?: boolean
  readonly sessions?: boolean
  readonly timezone?: string
  readonly fallbackRange?: UsageRangeSelection
  readonly cachedSnapshot?: UsageSnapshot | null
  readonly signal?: AbortSignal
}

export interface ReadinessReport {
  readonly status?: string
  readonly ready?: boolean
  readonly gatewayUrl?: string
  readonly [key: string]: unknown
}

export interface GatewayLogStatus {
  readonly gateway_file_log?: {
    readonly enabled?: boolean
    readonly path?: string
  }
  readonly raw_turn_call_log?: {
    readonly enabled?: boolean
    readonly source?: string
    readonly directory?: { readonly path?: string }
  }
  readonly diagnostics_enabled?: {
    readonly effective?: boolean
    readonly detail?: string
  }
}

export interface GatewayLogRecord {
  readonly level?: string
  readonly lvl?: string
  readonly message?: string
  readonly msg?: string
  readonly timestamp?: string | number
  readonly ts?: string | number
  readonly raw?: string
  readonly [key: string]: unknown
}

export type GatewayLogEntry = string | GatewayLogRecord

export interface GatewayLogBatch {
  readonly entries: readonly GatewayLogEntry[]
  readonly cursor: number | null
}

export interface UpdateNotice {
  readonly current?: string
  readonly latest?: string
  readonly available?: boolean
  readonly url?: string
}

export interface SupportBundle {
  readonly blob: Blob
  readonly filename: string
}

export interface Observability {
  usage(
    range: UsageRangeSelection,
    options?: UsageSnapshotOptions,
  ): Promise<UsageSnapshot>
  readiness(options: {
    readonly deep: boolean
    readonly agentId?: string
    readonly signal?: AbortSignal
  }): Promise<ReadinessReport>
  logStatus(options?: { readonly signal?: AbortSignal }): Promise<GatewayLogStatus>
  tailLogs(options: {
    readonly cursor: number
    readonly limit?: number
    readonly level?: string | null
    readonly signal?: AbortSignal
  }): Promise<GatewayLogBatch>
  updateNotice(options?: { readonly signal?: AbortSignal }): Promise<UpdateNotice | null | undefined>
  downloadSupportBundle(options: {
    readonly includeContent: boolean
    readonly days?: number
    readonly signal?: AbortSignal
  }): Promise<SupportBundle>
}

export const OBSERVABILITY_KEY: InjectionKey<Observability> = Symbol('Observability')
