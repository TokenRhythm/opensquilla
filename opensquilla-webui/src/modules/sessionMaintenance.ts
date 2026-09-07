import type { InjectionKey } from 'vue'

export interface SessionMaintenanceRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
  timeoutAction?: 'reject' | 'reconnect'
  abortAction?: 'reject' | 'reconnect'
}

export interface ResetSessionCommand {
  key: string
  force?: boolean
  options?: SessionMaintenanceRequestOptions
}

export interface ResetSessionResult {
  key: string
  reset: true
  rotated: boolean
  previousSessionId: string
  sessionId: string
  epoch: number
  flushReceipt?: Readonly<Record<string, unknown>> | null
}

export interface CompactSessionCommand {
  key: string
  wait?: boolean
  contextWindowTokens?: number
  instructions?: string
  options?: SessionMaintenanceRequestOptions
}

export interface SessionCompactionResult {
  key: string
  compactionId: string
  status: string
  compacted: boolean
  applied: boolean
  durability: string
  userVisible: boolean
  mode?: string
  summaryLength?: number
  summarySource?: string
  contextWindowTokens?: number
  tokensBefore?: number
  tokensAfter?: number
  remainingBudgetTokens?: number
  removedCount?: number
  keptCount?: number
  chunkCount?: number
  coverageStatus?: string
  missingObligationCount?: number
  criticalCarryForwardCount?: number
  stateKind?: string
  qualityReport?: Readonly<Record<string, unknown>>
  skipReason?: string
  reason?: string
  flushReceipt?: Readonly<Record<string, unknown>> | null
  flushReceiptStatus?: string | null
}

/** Business-facing maintenance seam; v4 aliases and wire fields stay private. */
export interface SessionMaintenance {
  reset(command: ResetSessionCommand): Promise<ResetSessionResult>
  compact(command: CompactSessionCommand): Promise<SessionCompactionResult>
}

export const SESSION_MAINTENANCE_KEY: InjectionKey<SessionMaintenance> =
  Symbol('SessionMaintenance')
