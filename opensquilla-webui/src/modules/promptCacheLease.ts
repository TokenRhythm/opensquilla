import type { InjectionKey } from 'vue'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'

export interface SetPromptCacheLease {
  readonly key: string
  readonly enabled: boolean
  readonly ttlSeconds: number
  readonly idleTimeoutSeconds: number
}

export interface PromptCacheLease {
  isAvailable(): boolean
  status(key: string): Promise<PromptCacheKeepaliveStatus>
  setPolicy(command: SetPromptCacheLease): Promise<PromptCacheKeepaliveStatus>
}

export const PROMPT_CACHE_LEASE_KEY: InjectionKey<PromptCacheLease> =
  Symbol('PromptCacheLease')
