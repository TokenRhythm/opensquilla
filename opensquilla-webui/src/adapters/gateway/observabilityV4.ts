import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  LOGS_TAIL_METHOD,
  type Result as LogsTailResult,
} from '@/contracts/generated/v4/logsTail'
import { validateResult as validateLogsTailResult } from '@/contracts/generated/v4/logsTailValidators.mjs'
import { createV4UsageReporting } from './usageReportingV4'
import type { SupportBundleUnavailableReason } from '@/modules/gatewayAccess'
import type {
  Observability,
  UpdateNotice,
} from '@/modules/observability'

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

export function createV4Observability(
  rpc: RpcTransport,
  http: HttpTransport,
  supportBundleUnavailableReason: () => SupportBundleUnavailableReason,
): Observability {
  const usageReporting = createV4UsageReporting(rpc)
  return {
    usage(range, options = {}) {
      return usageReporting.snapshot(range, options)
    },
    async tailLogs(options) {
      await rpc.ready({ signal: options?.signal })
      const result = await rpc.request<LogsTailResult>(
        LOGS_TAIL_METHOD,
        { cursor: 0, limit: 200, level: null },
        {
          timeoutMs: 15_000,
          timeoutAction: 'reject',
          abortAction: 'reject',
          ...(options?.signal ? { signal: options.signal } : {}),
        },
      )
      if (!validateLogsTailResult(result)) throw new Error(`${LOGS_TAIL_METHOD} returned an invalid response`)
      return { entries: result.lines, truncated: result.has_more }
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
      // Recheck the same capability used by the UI immediately before HTTP.
      // A Gateway switch must not send its new token to the page's old origin.
      const unavailable = supportBundleUnavailableReason()
      if (unavailable !== null) throw new Error(`Support bundle unavailable: ${unavailable}`)
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
