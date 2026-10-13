import type { ProviderRecovery } from '@/types/parts'

/** Only an explicit physical HTTP response can supply this display fact. */
export function providerHttpStatus(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value >= 400 && value <= 599
    ? value : undefined
}

const REASONS = new Set([
  'initial', 'rate_limited', 'provider_overloaded', 'transport_transient',
  'reasoning_only', 'empty_response', 'stream_incomplete', 'invalid_response', 'context_overflow', 'unknown',
])
function integer(value: unknown, max: number): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= max
    ? value : undefined
}

/** Display identifiers, never provider exceptions or inferred model choices. */
export function providerActivityModel(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const model = value.trim()
  return model && [...model].length <= 256 && !/[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/.test(model)
    ? model : undefined
}

/** Bounded presentation metadata, independent of local arrival time. */
export function normalizeProviderRecovery(raw: unknown): ProviderRecovery | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  const r = raw as Record<string, unknown>
  if (!['requesting', 'reasoning', 'retry_wait', 'retrying', 'fallback'].includes(String(r.phase))) return undefined
  const phase = r.phase as ProviderRecovery['phase']
  const startedAt = integer(r.started_at ?? r.startedAt, 10_000_000_000_000)
  const model = providerActivityModel(r.model)
  const fromModel = phase === 'fallback' ? providerActivityModel(r.from_model ?? r.fromModel) : undefined
  return {
    phase,
    ...(model ? { model } : {}),
    ...(fromModel ? { fromModel } : {}),
    reason: typeof r.reason === 'string' && REASONS.has(r.reason) ? r.reason : 'unknown',
    ...(typeof (r.activity_id ?? r.activityId) === 'string'
      ? { activityId: String(r.activity_id ?? r.activityId).slice(0, 240) } : {}),
    startedAt: startedAt && startedAt > 0 ? startedAt : undefined,
    retryAfterMs: phase === 'retry_wait' ? integer(r.retry_after_ms ?? r.retryAfterMs, 900_000) : undefined,
    attempt: integer(r.retry_attempt ?? r.attempt, 10_000),
    limit: integer(r.retry_limit ?? r.limit, 10_000),
    httpStatus: phase === 'retry_wait' ? providerHttpStatus(r.http_status ?? r.httpStatus) : undefined,
  }
}

export function providerRecoveryIdentity(value: ProviderRecovery | undefined): string {
  if (!value) return ''
  return JSON.stringify([value.activityId, value.phase, value.reason, value.attempt, value.startedAt, value.retryAfterMs, value.model, value.fromModel])
}

/** No timer inference from arrival time, stale history, or implausible clocks. */
export function providerRetrySeconds(value: ProviderRecovery | undefined, now = Date.now()): number | null {
  if (value?.phase !== 'retry_wait' || !value.startedAt || !value.retryAfterMs) return null
  if (!Number.isFinite(now) || value.startedAt > now + 5_000) return null
  const remaining = Math.ceil((value.startedAt + value.retryAfterMs - now) / 1000)
  return remaining > 0 ? remaining : null
}
