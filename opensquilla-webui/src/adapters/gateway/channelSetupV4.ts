import type { RpcCallOptions } from '@/lib/rpc'
import {
  stripChannelRedactionSentinels,
  type ChannelMutationOutcome,
  type ChannelSetup,
} from '@/modules/channelSetup'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
}

interface WireMutation {
  changed?: boolean
  restartRequired?: boolean
  liveApply?: Record<string, string> | null
  entry?: { name?: string }
}

const outcome = (name: string, result?: WireMutation | null): ChannelMutationOutcome => ({
  name: name || String(result?.entry?.name || ''),
  changed: result?.changed !== false,
  restartRequired: result?.restartRequired !== false,
  liveApplyFailed: Boolean(name) && result?.liveApply?.[name] === 'failed',
})

export function createV4ChannelSetup(rpc: RpcTransport): ChannelSetup {
  return {
    probeDraft(entry) {
      return rpc.request('onboarding.channel.probe', {
        entry: stripChannelRedactionSentinels(entry),
      })
    },
    async upsert(entry) {
      const name = String(entry.name || '')
      return outcome(name, await rpc.request('onboarding.channel.upsert', {
        entry: stripChannelRedactionSentinels(entry),
      }))
    },
    async remove(name) {
      return outcome(name, await rpc.request('onboarding.channel.remove', { name }))
    },
    async setEnabled(name, enabled) {
      return outcome(name, await rpc.request(
        `onboarding.channel.${enabled ? 'enable' : 'disable'}`,
        { name },
      ))
    },
  }
}
