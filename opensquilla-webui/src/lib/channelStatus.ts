// Single source of truth for user-visible channel state. Both surfaces
// (Settings runtime list and the /channels monitor page) render channel state
// through statusPresentation(), so the vocabulary cannot drift between them.
//
// The gateway emits exactly six wire statuses (rpc_channels.py `_status_for`):
// connected | stopped | disabled | dead | exhausted | restarting. Anything
// else falls through to the translated `unknown` presentation — a raw enum
// must never reach the user unlabeled.

export type ChannelStatusTone = 'ok' | 'info' | 'muted' | 'danger'

export type ChannelStatusKey =
  | 'connected'
  | 'pendingRestart'
  | 'notRunning'
  | 'disabled'
  | 'restarting'
  | 'failed'
  | 'exhausted'
  | 'unknown'

export interface ChannelStatusInput {
  status?: string | null
  enabled?: boolean | null
  connected?: boolean | null
  /** Client-side overlay: a saved change for this channel awaits a gateway restart. */
  pendingRestart?: boolean | null
  /** `diagnostics.last_error.error_class` when present. */
  errorClass?: string | null
  /** `diagnostics.last_error` records a startup failure (see startupFailure). */
  startupFailed?: boolean | null
}

export interface ChannelStatusPresentation {
  key: ChannelStatusKey
  labelKey: string
  tone: ChannelStatusTone
  hintKey?: string
  causeKey?: string
  /** Unrecognized wire value, interpolated into the `unknown` label. */
  raw?: string
}

/** Severity order shared by row sorting and the summary chips. */
export const CHANNEL_STATUS_ORDER: readonly ChannelStatusKey[] = [
  'failed',
  'exhausted',
  'restarting',
  'pendingRestart',
  'connected',
  'notRunning',
  'disabled',
  'unknown',
]

// The delivery-journal error taxonomy (channels/contract.py). Unknown classes
// simply get no cause line.
const ERROR_CLASSES = new Set([
  'auth_invalid',
  'payload_rejected',
  'target_missing',
  'contract_violation',
  'transport_transient',
  'rate_limited',
  'channel_degraded',
  // Synthesized by the gateway when an adapter fails during startup.
  'startup_failed',
])

function causeKeyFor(errorClass?: string | null): string | undefined {
  return errorClass && ERROR_CLASSES.has(errorClass)
    ? `channelStatus.cause.${errorClass}`
    : undefined
}

export function statusPresentation(input: ChannelStatusInput): ChannelStatusPresentation {
  const causeKey = causeKeyFor(input.errorClass)
  if (input.pendingRestart) {
    return {
      key: 'pendingRestart',
      labelKey: 'channelStatus.pendingRestart',
      tone: 'info',
      hintKey: 'channelStatus.hint.pendingRestart',
    }
  }
  if (input.enabled === false || input.status === 'disabled') {
    // Deliberately switched off by the operator — never styled as a problem.
    return { key: 'disabled', labelKey: 'channelStatus.disabled', tone: 'muted' }
  }
  switch (input.status) {
    case 'connected':
      return { key: 'connected', labelKey: 'channelStatus.connected', tone: 'ok' }
    case 'restarting':
      return {
        key: 'restarting',
        labelKey: 'channelStatus.restarting',
        tone: 'info',
        hintKey: 'channelStatus.hint.restarting',
        causeKey,
      }
    case 'dead':
      return {
        key: 'failed',
        labelKey: 'channelStatus.failed',
        tone: 'danger',
        hintKey: 'channelStatus.hint.failed',
        causeKey,
      }
    case 'exhausted':
      return {
        key: 'exhausted',
        labelKey: 'channelStatus.exhausted',
        tone: 'danger',
        hintKey: 'channelStatus.hint.exhausted',
        causeKey,
      }
    case 'stopped':
      // A channel that failed to START also reports "stopped" on the wire;
      // the startup error in diagnostics is what separates "operator left it
      // idle" (muted) from "it tried and could not boot" (danger).
      if (input.startupFailed) {
        return {
          key: 'failed',
          labelKey: 'channelStatus.failed',
          tone: 'danger',
          hintKey: 'channelStatus.hint.failed',
          causeKey,
        }
      }
      return {
        key: 'notRunning',
        labelKey: 'channelStatus.notRunning',
        tone: 'muted',
        hintKey: 'channelStatus.hint.notRunning',
      }
  }
  return {
    key: 'unknown',
    labelKey: 'channelStatus.unknown',
    tone: 'muted',
    raw: String(input.status ?? ''),
  }
}

/** Pull `last_error.error_class` out of a channel's diagnostics payload. */
export function lastErrorClass(diagnostics: unknown): string | undefined {
  if (!diagnostics || typeof diagnostics !== 'object') return undefined
  const lastError = (diagnostics as Record<string, unknown>).last_error
  if (!lastError || typeof lastError !== 'object') return undefined
  const errorClass = (lastError as Record<string, unknown>).error_class
  return typeof errorClass === 'string' && errorClass ? errorClass : undefined
}

/**
 * Whether a channel's diagnostics record a startup failure: the gateway
 * attaches `last_error` with `source: "start_error"` (adapter-provided
 * diagnostics) or `error_class: "startup_failed"` (synthesized) when the
 * adapter raised during start — while the wire status stays "stopped".
 */
export function startupFailure(diagnostics: unknown): boolean {
  if (!diagnostics || typeof diagnostics !== 'object') return false
  const lastError = (diagnostics as Record<string, unknown>).last_error
  if (!lastError || typeof lastError !== 'object') return false
  const record = lastError as Record<string, unknown>
  return record.source === 'start_error' || record.error_class === 'startup_failed'
}

/**
 * Whether the gateway process has actually loaded this channel's adapter.
 * `stopped` with no capability profile means the entry exists only in config —
 * restarting it can only fail until the gateway itself restarts.
 */
export function adapterLoaded(row: {
  status?: string | null
  capability_profile?: unknown
}): boolean {
  if (row.capability_profile != null) return true
  const status = String(row.status ?? '')
  return status === 'connected' || status === 'restarting' || status === 'dead' || status === 'exhausted'
}
