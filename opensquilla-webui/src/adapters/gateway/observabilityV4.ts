import type { RpcCallOptions } from '@/lib/rpc'
import type {
  GatewayLogBatch,
  GatewayLogEntry,
  GatewayLogStatus,
  Observability,
  ReadinessReport,
  UpdateNotice,
} from '@/modules/observability'
import type {
  UsageQueryResponse,
  UsageRangeSelection,
  UsageStatusData,
} from '@/types/usage'
import {
  browserTimeZone,
  normalizeUsageQueryResponse,
  normalizeUsageStatusResponse,
  usagePresetForRange,
} from '@/composables/usage/useUsageQuery'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { signal?: AbortSignal }): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

interface HttpTransport {
  requestJson<T>(endpoint: string, options?: {
    method?: 'GET'
    timeoutMs?: number
    signal?: AbortSignal
  }): Promise<T>
  requestBinary(endpoint: string, options: {
    method: 'POST'
    json: unknown
    signal?: AbortSignal
  }): Promise<{
    readonly metadata: { readonly filename?: string }
    blob(): Promise<Blob>
  }>
}

const USAGE_QUERY_METHOD = 'usage.query'
const USAGE_STATUS_METHOD = 'usage.status'
const DOCTOR_STATUS_METHOD = 'doctor.status'
const LOGS_STATUS_METHOD = 'logs.status'
const LOGS_TAIL_METHOD = 'logs.tail'

const callOptions = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

function methodNotFound(error: unknown): boolean {
  const code = typeof error === 'object' && error && 'code' in error
    ? String((error as { code?: unknown }).code ?? '')
    : ''
  const message = error instanceof Error ? error.message : String(error)
  return code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

function invalidTimezone(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /unknown iana timezone|invalid timezone|time zone/i.test(message)
}

function usageQueryParams(
  range: UsageRangeSelection,
  timezone: string,
  options: Parameters<Observability['usage']>[1],
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    range: { preset: usagePresetForRange(range) },
    timezone,
    include: {
      days: options?.days ?? true,
      models: options?.models ?? true,
      sessions: options?.sessions ?? true,
    },
  }
}

function updateNotice(value: unknown): UpdateNotice | null | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = value as Record<string, unknown>
  if (
    typeof raw.current !== 'string'
    || typeof raw.available !== 'boolean'
    || (raw.latest !== null && typeof raw.latest !== 'string')
    || (raw.url !== null && typeof raw.url !== 'string')
    || (raw.checkedAt !== null && typeof raw.checkedAt !== 'string')
  ) return undefined
  if (!raw.available) return null
  if (typeof raw.latest !== 'string' || !raw.latest.trim()) return undefined
  return {
    current: raw.current,
    latest: raw.latest,
    available: true,
    url: typeof raw.url === 'string' && raw.url ? raw.url : undefined,
  }
}

export function createV4Observability(rpc: RpcTransport, http: HttpTransport): Observability {
  return {
    async usage(range, options = {}) {
      await rpc.ready({ signal: options.signal })
      const timezone = options.timezone || browserTimeZone()
      const requestedPreset = usagePresetForRange(range)
      const matchingLedgerCache = options.cachedSnapshot?.source === 'usage_ledger'
        && options.cachedSnapshot.range.preset === requestedPreset
        ? options.cachedSnapshot
        : null
      let transientQueryFailure = false
      if (rpc.supports(USAGE_QUERY_METHOD)) {
        try {
          return normalizeUsageQueryResponse(await rpc.request<UsageQueryResponse>(
            USAGE_QUERY_METHOD,
            usageQueryParams(range, timezone, options),
            callOptions(options.signal),
          ))
        } catch (error) {
          if (methodNotFound(error)) {
            rpc.markUnsupported(USAGE_QUERY_METHOD)
          } else if (timezone !== 'UTC' && invalidTimezone(error)) {
            try {
              const snapshot = normalizeUsageQueryResponse(await rpc.request<UsageQueryResponse>(
                USAGE_QUERY_METHOD,
                usageQueryParams(range, 'UTC', options),
                callOptions(options.signal),
              ))
              return {
                ...snapshot,
                timezoneFallback: {
                  requestedTimezone: timezone,
                  effectiveTimezone: snapshot.timezone,
                  reason: 'invalid_timezone',
                },
              }
            } catch (utcError) {
              if (methodNotFound(utcError)) rpc.markUnsupported(USAGE_QUERY_METHOD)
              else transientQueryFailure = true
            }
          } else {
            transientQueryFailure = true
          }
        }
      }
      try {
        const status = await rpc.request<UsageStatusData>(
          USAGE_STATUS_METHOD,
          undefined,
          callOptions(options.signal),
        )
        if (transientQueryFailure && matchingLedgerCache) return matchingLedgerCache
        return normalizeUsageStatusResponse(
          status,
          options.fallbackRange || range,
          timezone,
        )
      } catch (error) {
        if (matchingLedgerCache) return matchingLedgerCache
        throw error
      }
    },
    async readiness(options) {
      await rpc.ready({ signal: options.signal })
      return asRecord(await rpc.request(
        DOCTOR_STATUS_METHOD,
        { agentId: options.agentId || 'main', deep: options.deep },
        callOptions(options.signal),
      )) as ReadinessReport
    },
    async logStatus(options) {
      await rpc.ready({ signal: options?.signal })
      return asRecord(await rpc.request(
        LOGS_STATUS_METHOD,
        {},
        callOptions(options?.signal),
      )) as GatewayLogStatus
    },
    async tailLogs(options) {
      await rpc.ready({ signal: options.signal })
      const raw = asRecord(await rpc.request(
        LOGS_TAIL_METHOD,
        {
          cursor: options.cursor,
          limit: options.limit ?? 500,
          level: options.level ?? null,
        },
        callOptions(options.signal),
      ))
      const entries = Array.isArray(raw.lines)
        ? raw.lines
        : Array.isArray(raw.entries) ? raw.entries : []
      return {
        entries: entries.filter(item => typeof item === 'string'
          || (item !== null && typeof item === 'object' && !Array.isArray(item))) as GatewayLogEntry[],
        cursor: Number.isInteger(raw.cursor) ? Number(raw.cursor) : null,
      } satisfies GatewayLogBatch
    },
    async updateNotice(options) {
      try {
        return updateNotice(await http.requestJson('/api/system/update', {
          method: 'GET',
          timeoutMs: 5_000,
          signal: options?.signal,
        }))
      } catch {
        return undefined
      }
    },
    async downloadSupportBundle(options) {
      const response = await http.requestBinary('/api/v1/diagnostics/bundle', {
        method: 'POST',
        json: {
          include_content: options.includeContent,
          days: options.days ?? 1,
        },
        signal: options.signal,
      })
      return {
        blob: await response.blob(),
        filename: response.metadata.filename || 'opensquilla-bundle.zip',
      }
    },
  }
}
