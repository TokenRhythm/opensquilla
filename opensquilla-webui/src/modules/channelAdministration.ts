import type { InjectionKey } from 'vue'

export interface ChannelCapabilityEvidence {
  readonly declared?: boolean
  readonly implemented?: boolean
  readonly effective?: boolean
  readonly evidence_kind?: string
  readonly methods?: readonly string[]
  readonly proof_status?: string
}

export interface Channel {
  readonly name?: string
  readonly id?: string
  readonly type?: string
  readonly status?: string
  readonly connected?: boolean
  readonly connected_since?: string | number | null
  readonly restart_attempts?: number
  readonly pendingPairings?: number
  readonly bot_user_id?: string | null
  readonly enabled?: boolean
  readonly configured?: boolean
  readonly capabilities?: readonly string[]
  readonly capability_profile?: {
    readonly transports?: readonly string[]
    readonly maturity?: string
    readonly evidence?: Readonly<Record<string, ChannelCapabilityEvidence>>
  } | null
  readonly diagnostics?: Readonly<Record<string, unknown>>
  readonly [key: string]: unknown
}

export interface ProbeResult {
  readonly status: string
  readonly connected: boolean
  readonly latencyMs?: number | null
  readonly detail?: string
  readonly result?: Readonly<Record<string, unknown>>
}

export interface ChannelPairing {
  readonly pairingId: string
  readonly pairingCode?: string
  readonly channelName: string
  readonly senderId: string
  readonly senderName?: string | null
  readonly status: 'pending' | 'approved' | string
  readonly createdAt?: string | null
  readonly approvedAt?: string | null
}

export interface PairingApproval {
  readonly adminGranted?: boolean
  readonly warnings?: readonly string[]
}

export interface ChannelStatusSubscription {
  close(): void
}

export interface ChannelAdministration {
  status(): Promise<readonly Channel[]>
  get(name: string): Promise<{
    readonly entry: Record<string, unknown> | null
    readonly secretFields: readonly string[]
  }>
  probe(name: string): Promise<ProbeResult>
  restart(name: string): Promise<void>
  logout(name: string): Promise<void>
  listPairings(name: string): Promise<readonly ChannelPairing[]>
  approvePairing(name: string, pairingId: string, asAdmin: boolean): Promise<PairingApproval>
  revokePairing(name: string, pairingId: string): Promise<void>
  setAdmin(name: string, senderId: string, admin: boolean): Promise<void>
  ready(): Promise<void>
  subscribeStatus(listener: () => void): ChannelStatusSubscription
}

export const CHANNEL_ADMINISTRATION_KEY: InjectionKey<ChannelAdministration> = Symbol('ChannelAdministration')
