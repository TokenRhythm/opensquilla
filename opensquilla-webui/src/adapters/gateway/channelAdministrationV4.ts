import type { RpcCallOptions } from '@/lib/rpc'
import type {
  ChannelAdministration,
  Channel,
  ChannelPairing,
  PairingApproval,
  ProbeResult,
} from '@/modules/channelAdministration'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { signal?: AbortSignal }): Promise<void>
}

interface EventTransport {
  subscribe(event: string, handler: (payload: unknown) => void): { close(): void }
}

const record = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

export function createV4ChannelAdministration(
  rpc: RpcTransport,
  events: EventTransport,
): ChannelAdministration {
  return {
    async status() {
      await rpc.ready()
      const result = record(await rpc.request('channels.status'))
      return Array.isArray(result.channels) ? result.channels as Channel[] : []
    },
    async get(name) {
      const result = record(await rpc.request('channels.get', { name }))
      return {
        entry: result.entry && typeof result.entry === 'object' && !Array.isArray(result.entry)
          ? result.entry as Record<string, unknown>
          : null,
        secretFields: Array.isArray(result.secretFields)
          ? result.secretFields.map(String)
          : [],
      }
    },
    probe(name) {
      return rpc.request<ProbeResult>('channels.probe', { name })
    },
    async restart(name) {
      await rpc.request('channels.restart', { name })
    },
    async logout(name) {
      await rpc.request('channels.logout', { name })
    },
    async listPairings(name) {
      const result = record(await rpc.request('channels.pairings', { channelName: name }))
      return Array.isArray(result.pairings)
        ? (result.pairings as ChannelPairing[]).filter(pairing => pairing.channelName === name)
        : []
    },
    approvePairing(name, pairingId, asAdmin) {
      return rpc.request<PairingApproval>('channels.pairing.approve', {
        channelName: name,
        pairingId,
        ...(asAdmin ? { asAdmin: true } : {}),
      })
    },
    async revokePairing(name, pairingId) {
      await rpc.request('channels.pairing.revoke', { channelName: name, pairingId })
    },
    async setAdmin(name, senderId, admin) {
      await rpc.request('channels.admin.set', { channelName: name, senderId, admin })
    },
    ready() {
      return rpc.ready()
    },
    subscribeStatus(listener) {
      return events.subscribe('channel.status', listener)
    },
  }
}
