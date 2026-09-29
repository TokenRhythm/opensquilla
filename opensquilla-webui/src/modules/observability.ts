import type { InjectionKey } from 'vue'
import type { TraceDetails, TraceProjection } from '@/types/traceView'
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

export interface UpdateNotice {
  readonly current?: string
  readonly latest?: string
  readonly available?: boolean
  readonly url?: string
}

export type GatewayLogEntry = string | Readonly<Record<string, unknown>>

export interface GatewayLogBatch {
  readonly entries: readonly GatewayLogEntry[]
  readonly truncated: boolean
}

export interface SupportBundle {
  readonly blob: Blob
  readonly filename: string
}

export interface TurnTraceSummary {
  trace_id: string
  started_at?: string
  status: string
  complete: boolean
  raw_available: boolean
}

export interface TurnTracesSnapshot {
  traces: TurnTraceSummary[]
  raw_enabled: boolean
}

export interface TracePayloadSnapshot { available: boolean; payload: unknown }

export interface Observability {
  turnTraces(sessionKey: string, turnId: string, options?: { signal?: AbortSignal }): Promise<TurnTracesSnapshot>
  traceProjection(traceId: string, options?: { signal?: AbortSignal }): Promise<TraceProjection | null>
  traceDetails(traceId: string, options?: { signal?: AbortSignal; limit?: number }): Promise<TraceDetails | null>
  tracePayload(traceId: string, seq: number, options?: { signal?: AbortSignal }): Promise<TracePayloadSnapshot>
  usage(
    range: UsageRangeSelection,
    options?: UsageSnapshotOptions,
  ): Promise<UsageSnapshot>
  /** Read the latest 200 lines once from the currently connected Gateway. */
  tailLogs(options?: { readonly signal?: AbortSignal }): Promise<GatewayLogBatch>
  updateNotice(options?: { readonly signal?: AbortSignal }): Promise<UpdateNotice | null | undefined>
  downloadSupportBundle(options: {
    readonly includeContent: boolean
    readonly days?: number
    readonly signal?: AbortSignal
  }): Promise<SupportBundle>
}

export const OBSERVABILITY_KEY: InjectionKey<Observability> = Symbol('Observability')
