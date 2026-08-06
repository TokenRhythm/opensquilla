export type PromptCacheKeepaliveState =
  | 'off'
  | 'waiting'
  | 'scheduled'
  | 'probing'
  | 'paused'
  | 'stopped'

export interface PromptCacheKeepaliveStatus {
  enabled: boolean
  ttlSeconds: number
  intervalSeconds: number
  idleTimeoutSeconds?: number
  idleExpiresAt?: number | null
  state: PromptCacheKeepaliveState
  reason?: string | null
  hasSnapshot: boolean
  nextProbeAt?: number | null
  lastProbeAt?: number | null
  lastCacheHitTokens: number
  provider?: string | null
  model?: string | null
}

export interface PromptCacheKeepaliveStatusUpdate {
  sessionKey: string
  status: PromptCacheKeepaliveStatus
}
