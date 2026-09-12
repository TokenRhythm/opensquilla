import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  STATUS_METHOD,
  type Result as RuntimeStatusResult,
} from '@/contracts/generated/v4/runtimeStatus'
import { validateResult as validateRuntimeStatusResult } from '@/contracts/generated/v4/runtimeStatusValidators.mjs'
import {
  ROUTER_SELFLEARNING_STATUS_METHOD,
  type Result as RouterSelflearningStatusResult,
} from '@/contracts/generated/v4/routerSelflearningStatus'
import { validateResult as validateRouterSelflearningStatusResult } from '@/contracts/generated/v4/routerSelflearningStatusValidators.mjs'
import {
  DOCTOR_STATUS_METHOD,
  type Result as DoctorStatusResult,
} from '@/contracts/generated/v4/doctorStatus'
import { validateResult as validateDoctorStatusResult } from '@/contracts/generated/v4/doctorStatusValidators.mjs'
import {
  LOGS_STATUS_METHOD,
  type Result as LogsStatusResult,
} from '@/contracts/generated/v4/logsStatus'
import { validateResult as validateLogsStatusResult } from '@/contracts/generated/v4/logsStatusValidators.mjs'
import {
  LOGS_TAIL_METHOD,
  type Result as LogsTailResult,
} from '@/contracts/generated/v4/logsTail'
import { validateResult as validateLogsTailResult } from '@/contracts/generated/v4/logsTailValidators.mjs'
import { createV4UsageReporting } from './usageReportingV4'
import type {
  GatewayLogBatch,
  GatewayLogEntry,
  GatewayLogStatus,
  GatewayStatus,
  Observability,
  ReadinessReport,
  SelfLearningStatus,
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

const callOptions = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
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
  const usageReporting = createV4UsageReporting(rpc)
  return {
    async gatewayStatus(options) {
      await rpc.ready({ signal: options?.signal })
      const result = await rpc.request<RuntimeStatusResult>(
        STATUS_METHOD,
        {},
        callOptions(options?.signal),
      )
      if (!validateRuntimeStatusResult(result)) throw invalid(STATUS_METHOD)
      return result as GatewayStatus
    },
    async selfLearningStatus(options) {
      await rpc.ready({ signal: options?.signal })
      const result = await rpc.request<RouterSelflearningStatusResult>(
        ROUTER_SELFLEARNING_STATUS_METHOD,
        {},
        callOptions(options?.signal),
      )
      if (!validateRouterSelflearningStatusResult(result)) {
        throw invalid(ROUTER_SELFLEARNING_STATUS_METHOD)
      }
      return result as SelfLearningStatus
    },
    usage(range, options = {}) {
      return usageReporting.snapshot(range, options)
    },
    async readiness(options) {
      await rpc.ready({ signal: options.signal })
      const result = await rpc.request<DoctorStatusResult>(
        DOCTOR_STATUS_METHOD,
        { agentId: options.agentId || 'main', deep: options.deep },
        callOptions(options.signal),
      )
      if (!validateDoctorStatusResult(result)) throw invalid(DOCTOR_STATUS_METHOD)
      return result as ReadinessReport
    },
    async logStatus(options) {
      await rpc.ready({ signal: options?.signal })
      const result = await rpc.request<LogsStatusResult>(
        LOGS_STATUS_METHOD,
        {},
        callOptions(options?.signal),
      )
      if (!validateLogsStatusResult(result)) throw invalid(LOGS_STATUS_METHOD)
      return result as GatewayLogStatus
    },
    async tailLogs(options) {
      await rpc.ready({ signal: options.signal })
      const raw = await rpc.request<LogsTailResult>(
        LOGS_TAIL_METHOD,
        {
          cursor: options.cursor,
          limit: options.limit ?? 500,
          level: options.level ?? null,
        },
        callOptions(options.signal),
      )
      if (!validateLogsTailResult(raw)) throw invalid(LOGS_TAIL_METHOD)
      return {
        entries: raw.lines as GatewayLogEntry[],
        cursor: raw.cursor,
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
