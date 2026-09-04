import {
  SESSIONS_PROMPT_CACHE_KEEPALIVE_STATUS_METHOD,
  type Params as PromptCacheStatusParams,
  type Result as PromptCacheStatusWireResult,
} from '@/contracts/generated/v4/promptCacheKeepaliveStatus'
import { validateResult as validatePromptCacheStatusResult } from '@/contracts/generated/v4/promptCacheKeepaliveStatusValidators.mjs'
import {
  SESSIONS_PROMPT_CACHE_KEEPALIVE_SET_METHOD,
  type Params as PromptCacheSetParams,
  type Result as PromptCacheSetWireResult,
} from '@/contracts/generated/v4/promptCacheKeepaliveSet'
import { validateResult as validatePromptCacheSetResult } from '@/contracts/generated/v4/promptCacheKeepaliveSetValidators.mjs'
import type { PromptCacheLease } from '@/modules/promptCacheLease'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'

interface PromptCacheLeaseTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  supports?(method: string): boolean
}

export function createV4PromptCacheLease(
  transport: PromptCacheLeaseTransport,
): PromptCacheLease {
  return {
    isAvailable: () => (
      transport.supports?.(SESSIONS_PROMPT_CACHE_KEEPALIVE_STATUS_METHOD) !== false
    ),
    async status(key) {
      const params: PromptCacheStatusParams = { key }
      const raw = await transport.request<PromptCacheStatusWireResult>(
        SESSIONS_PROMPT_CACHE_KEEPALIVE_STATUS_METHOD,
        params,
      )
      if (!validatePromptCacheStatusResult(raw)) {
        throw new Error(
          `${SESSIONS_PROMPT_CACHE_KEEPALIVE_STATUS_METHOD} returned an invalid response`,
        )
      }
      return raw as unknown as PromptCacheKeepaliveStatus
    },
    async setPolicy(command) {
      const params: PromptCacheSetParams = {
        key: command.key,
        enabled: command.enabled,
        ttlSeconds: command.ttlSeconds,
        idleTimeoutSeconds: command.idleTimeoutSeconds,
      }
      const raw = await transport.request<PromptCacheSetWireResult>(
        SESSIONS_PROMPT_CACHE_KEEPALIVE_SET_METHOD,
        params,
      )
      if (!validatePromptCacheSetResult(raw)) {
        throw new Error(
          `${SESSIONS_PROMPT_CACHE_KEEPALIVE_SET_METHOD} returned an invalid response`,
        )
      }
      return raw as unknown as PromptCacheKeepaliveStatus
    },
  }
}
