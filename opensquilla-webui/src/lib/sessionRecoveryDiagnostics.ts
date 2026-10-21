import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'

/** Content-free breadcrumbs accepted by the desktop's narrow warning sink. */
export const SESSION_RECOVERY_LOG_PREFIX = '[opensquilla-recovery] '

export type RecoveryReason =
  | 'server_dirty' | 'delivery_ledger_limit' | 'invalid_lane' | 'missing_session_key'
  | 'retired_lane_delivery' | 'stale_lane' | 'lane_history_limit'
  | 'consumer_timeout' | 'consumer_dirty' | 'consumer_rejected'
  | 'recovery_incomplete' | 'recovery_invalidated' | 'recovery_terminal'
  | 'recovery_completed' | 'recovery_exception'
  | 'live_subscription_failed' | 'metadata_hydration_failed' | 'metadata_recovery_failed'
  | 'transport_sequence_gap'

export interface SessionRecoveryDiagnostic {
  phase: 'trigger' | 'failed' | 'recovered'
  reason: RecoveryReason
  sessionKey?: string
  connectionGeneration?: number
  deliveryEpoch?: string
  deliveryId?: number
  streamSeq?: number
  expectedSeq?: number
  actualSeq?: number
  global?: boolean
  keyCount?: number
  error?: unknown
}

const ERROR_CODES = new Set([
  'SNAPSHOT_STALE', 'SNAPSHOT_BUSY', 'SNAPSHOT_TOO_LARGE', 'SNAPSHOT_UNAVAILABLE',
  'READ_UNAVAILABLE', 'REBASE_REQUIRED', 'BUSY', 'TIMEOUT', 'INVALID_REQUEST',
  'SESSION_NOT_FOUND', 'NOT_FOUND', 'METHOD_NOT_FOUND', 'DISCONNECTED',
  'RPC_TIMEOUT', 'RPC_ABORTED', 'RPC_TRANSPORT_ERROR', 'SNAPSHOT_EXPIRED',
  'READ_STALE', 'STORAGE_BUSY', 'HISTORY_CURSOR_INVALID', 'HISTORY_CURSOR_INVALIDATED',
])
const ERROR_KINDS = new Set([
  'aborted', 'timeout', 'busy', 'unavailable', 'too-large', 'budget-exhausted', 'rebase-required',
])

function identity(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= 160
    && /^[a-zA-Z0-9:_./-]+$/.test(value) ? value : undefined
}

function count(value: unknown): number | undefined {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : undefined
}

function failureFields(error: unknown): Record<string, unknown> {
  const fields: Record<string, unknown> = {}
  // Wrapped read failures retain a typed RPC cause. Never serialize messages,
  // stacks, arbitrary properties, or a provider's raw response.
  for (let depth = 0; depth < 3 && error && typeof error === 'object'; depth++) {
    const value = error as Record<string, unknown>
    if (typeof value.code === 'string' && ERROR_CODES.has(value.code)) fields.error_code = value.code
    if (typeof value.kind === 'string' && ERROR_KINDS.has(value.kind)) fields.error_kind ??= value.kind
    if (typeof value.retryable === 'boolean') fields.retryable ??= value.retryable
    error = value.cause
  }
  return fields
}

export class SessionRecoveryDiagnostics {
  private windowStart = 0
  private suppressed = 0
  // A chat owns its diagnostic allowance; visiting other chats must not consume
  // it. A fixed window admits at most 64 scopes, each with 8 distinct records:
  // at most 512 writes/signatures per fixed window, even if session IDs keep changing.
  // Do not evict scopes within the window: eviction would reset their allowance.
  private readonly scopes = new Map<string, Set<string>>()

  constructor(
    private readonly write: (message: string) => void = message => console.warn(message),
    private readonly now: () => number = Date.now,
  ) {}

  log(input: SessionRecoveryDiagnostic): void {
    try {
      this.writeDiagnostic(input)
    } catch {
      // Diagnostics cannot change delivery ownership or recovery success.
    }
  }

  private writeDiagnostic(input: SessionRecoveryDiagnostic): void {
    const now = this.now()
    if (now - this.windowStart >= 60_000 || now < this.windowStart) {
      this.windowStart = now
      this.scopes.clear()
    }
    const detail: Record<string, unknown> = {
      phase: input.phase,
      reason: input.reason,
      session_key_sha256: typeof input.sessionKey === 'string' && input.sessionKey.length <= 4096
        ? bytesToHex(sha256(new TextEncoder().encode(input.sessionKey))).slice(0, 16) : undefined,
      connection_generation: count(input.connectionGeneration),
      delivery_epoch: identity(input.deliveryEpoch),
      delivery_id: count(input.deliveryId),
      stream_seq: count(input.streamSeq),
      expected_seq: count(input.expectedSeq),
      actual_seq: count(input.actualSeq),
      global: input.global,
      key_count: count(input.keyCount),
      ...failureFields(input.error),
    }
    const signature = JSON.stringify([
      detail.phase, detail.reason, detail.session_key_sha256, detail.connection_generation,
      detail.delivery_epoch, detail.global, detail.error_code, detail.error_kind,
    ])
    const scope = typeof detail.session_key_sha256 === 'string'
      && /^[a-f0-9]{16}$/.test(detail.session_key_sha256)
      ? detail.session_key_sha256 : 'global'
    let signatures = this.scopes.get(scope)
    if (!signatures) {
      if (this.scopes.size >= 64) {
        this.suppressed++
        return
      }
      signatures = new Set<string>()
      this.scopes.set(scope, signatures)
    }
    // Deduplication belongs to the hashed chat. suppressed_count remains the
    // document-wide aggregate carried by the next successfully written record.
    if (signatures.size >= 8 || signatures.has(signature)) {
      this.suppressed++
      return
    }
    signatures.add(signature)
    const suppressed = this.suppressed
    this.suppressed = 0
    this.write(SESSION_RECOVERY_LOG_PREFIX + JSON.stringify({
      ...detail, suppressed_count: suppressed,
    }))
  }
}

export const sessionRecoveryDiagnostics = new SessionRecoveryDiagnostics()
