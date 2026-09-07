import { createV4ClarificationSubmission } from '@/adapters/gateway/clarificationSubmissionV4'
import { createV4CommandCatalog } from '@/adapters/gateway/commandCatalogV4'
import type { RpcCallOptions } from '@/lib/rpc'
import type { ClarificationSubmission } from '@/modules/clarificationSubmission'
import type { CommandCatalog } from '@/modules/commandCatalog'
import type { PromptCacheLease } from '@/modules/promptCacheLease'
import type { RouteFeedback } from '@/modules/routeFeedback'

export interface AncillaryTestRpc {
  call(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<unknown>
}

function transport(rpc: AncillaryTestRpc) {
  return {
    request: <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => (options === undefined
      ? rpc.call(method, params)
      : rpc.call(method, params, options)) as Promise<T>,
  }
}

export function commandCatalogFromTestRpc(rpc: AncillaryTestRpc): CommandCatalog {
  return createV4CommandCatalog(transport(rpc))
}

export function clarificationSubmissionFromTestRpc(
  rpc: AncillaryTestRpc,
): ClarificationSubmission {
  return createV4ClarificationSubmission(transport(rpc))
}

export function routeFeedbackTestDouble(
  overrides: Partial<RouteFeedback> = {},
): RouteFeedback {
  return {
    submit: async () => ({ accepted: true }),
    ...overrides,
  }
}

export function promptCacheLeaseTestDouble(
  overrides: Partial<PromptCacheLease> = {},
): PromptCacheLease {
  return {
    isAvailable: () => true,
    status: async () => {
      throw new Error('PromptCacheLease.status was not configured for this test')
    },
    setPolicy: async () => {
      throw new Error('PromptCacheLease.setPolicy was not configured for this test')
    },
    ...overrides,
  }
}
