import type { RpcCallOptions } from '@/lib/rpc'
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
  type Params as UsageCostParams,
  type Result as UsageCostWireResult,
} from '@/contracts/generated/v4/usageCost'
import { validateResult as validateUsageCostResult } from '@/contracts/generated/v4/usageCostValidators.mjs'
import type {
  UsageCostBreakdown,
  UsageQueryResult,
  UsageReportQuery,
  UsageReporting,
  UsageReportingRequestOptions,
  UsageStatusResult,
} from '@/modules/usageReporting'

interface UsageReportingTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}
function options(value?: UsageReportingRequestOptions): RpcCallOptions | undefined {
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
  const mapped = options(requestOptions)
  return mapped
    ? transport.request<T>(method, params, mapped)
    : transport.request<T>(method, params)
}

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

function queryParams(query: UsageReportQuery): UsageQueryParams {
  return {
    ...(query.schemaVersion !== undefined ? { schemaVersion: query.schemaVersion } : {}),
    ...(query.range !== undefined ? { range: query.range } : {}),
    ...(query.timezone !== undefined ? { timezone: query.timezone } : {}),
    ...(query.include !== undefined ? { include: query.include } : {}),
  }
}

export function createV4UsageReporting(
  transport: UsageReportingTransport,
): UsageReporting {
  return {
    async status(sessionKey, requestOptions) {
      const params: UsageStatusParams | undefined = sessionKey ? { sessionKey } : undefined
      const raw = await request<UsageStatusWireResult>(
        transport,
        USAGE_STATUS_METHOD,
        params,
        requestOptions,
      )
      if (!validateUsageStatusResult(raw)) throw invalid(USAGE_STATUS_METHOD)
      return raw as UsageStatusResult
    },
    async query(query, requestOptions) {
      const raw = await request<UsageQueryWireResult>(
        transport,
        USAGE_QUERY_METHOD,
        queryParams(query),
        requestOptions,
      )
      if (!validateUsageQueryResult(raw)) throw invalid(USAGE_QUERY_METHOD)
      return raw as UsageQueryResult
    },
    async costBreakdown(query = {}, requestOptions) {
      const params: UsageCostParams = queryParams(query)
      const raw = await request<UsageCostWireResult>(
        transport,
        USAGE_COST_METHOD,
        params,
        requestOptions,
      )
      if (!validateUsageCostResult(raw)) throw invalid(USAGE_COST_METHOD)
      return raw as UsageCostBreakdown
    },
  }
}
