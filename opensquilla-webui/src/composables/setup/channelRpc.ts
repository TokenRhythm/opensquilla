// Shared channel-entry RPC edge: the ONE place that talks to
// onboarding.channel.probe / onboarding.channel.upsert and interprets their
// restart flags. The /channels in-place editor (useChannelEditor) calls the
// wrappers, and the draft form (useSetupChannelsForm) scrubs its payloads
// through stripRedactionSentinels, so the two layers cannot drift apart on
// sentinel handling or restart semantics.

/** The redaction placeholder channels.get uses for stored secrets. */
export const REDACTED_SENTINEL = '***'

/** Minimal structural view of the RPC store — keeps this module store-free. */
export interface ChannelRpcCaller {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
}

/**
 * Terminal scrub: the literal redaction placeholder must never reach a probe
 * or upsert payload — the backend would persist it verbatim as the credential.
 */
export function stripRedactionSentinels(entry: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(entry)) {
    if (value === REDACTED_SENTINEL) continue
    out[key] = value
  }
  return out
}

export interface ChannelProbeResponse {
  status?: string
  connected?: boolean
  probeKind?: string
  restartRequired?: boolean
  warnings?: string[]
}

export interface ChannelUpsertResponse {
  changed?: boolean
  restartRequired?: boolean
  liveApply?: Record<string, string> | null
  entry?: { name?: string }
  warnings?: string[]
}

/**
 * Validate a draft entry server-side (merge-aware: blank secrets resolve
 * against the stored entry, so keep-current drafts validate as what the
 * upsert would persist). Throws on an invalid entry.
 */
export function probeChannelEntry(
  rpc: ChannelRpcCaller,
  entry: Record<string, unknown>,
): Promise<ChannelProbeResponse> {
  return rpc.call<ChannelProbeResponse>('onboarding.channel.probe', {
    entry: stripRedactionSentinels(entry),
  })
}

/** Persist a channel entry (create or update, matched on entry.name). */
export function upsertChannelEntry(
  rpc: ChannelRpcCaller,
  entry: Record<string, unknown>,
): Promise<ChannelUpsertResponse> {
  return rpc.call<ChannelUpsertResponse>('onboarding.channel.upsert', {
    entry: stripRedactionSentinels(entry),
  })
}

export interface ChannelSaveOutcome {
  /** Saved entry name (from the submitted entry, else the response echo). */
  name: string
  changed: boolean
  /** Restart-gated save — absent flags mean restart-required (legacy gateways). */
  restartRequired: boolean
  /** The gateway applied the entry live and the adapter failed to start. */
  liveApplyFailed: boolean
}

/**
 * Interpret the upsert response's restart flags. Both `changed` and
 * `restartRequired` default to true when absent so older gateways (which
 * omit them) keep the conservative restart-pending behavior.
 */
export function parseUpsertOutcome(
  entryName: unknown,
  res: ChannelUpsertResponse | null | undefined,
): ChannelSaveOutcome {
  const name = String(entryName || res?.entry?.name || '')
  return {
    name,
    changed: res?.changed !== false,
    restartRequired: res?.restartRequired !== false,
    liveApplyFailed: Boolean(name) && res?.liveApply?.[name] === 'failed',
  }
}
