import type { InjectionKey } from 'vue'

/** The redaction placeholder returned for stored channel secrets. */
export const REDACTED_SENTINEL = '***'

export interface ChannelProbeOutcome {
  readonly status?: string
  readonly connected?: boolean
  readonly probeKind?: string
  readonly restartRequired?: boolean
  readonly warnings?: readonly string[]
}

export interface ChannelMutationOutcome {
  readonly name: string
  readonly changed: boolean
  readonly restartRequired: boolean
  readonly liveApplyFailed: boolean
}

export interface ChannelSetup {
  probeDraft(entry: Record<string, unknown>): Promise<ChannelProbeOutcome>
  upsert(entry: Record<string, unknown>): Promise<ChannelMutationOutcome>
  remove(name: string): Promise<ChannelMutationOutcome>
  setEnabled(name: string, enabled: boolean): Promise<ChannelMutationOutcome>
}

export function stripChannelRedactionSentinels(
  entry: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(entry).filter(([, value]) => value !== REDACTED_SENTINEL),
  )
}

export const CHANNEL_SETUP_KEY: InjectionKey<ChannelSetup> = Symbol('ChannelSetup')
