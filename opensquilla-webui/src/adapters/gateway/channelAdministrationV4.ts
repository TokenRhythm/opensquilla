import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { CHANNELS_STATUS_METHOD, type Result as ChannelsStatusResult } from '@/contracts/generated/v4/channelsStatus'
import { validateResult as validateChannelsStatusResult } from '@/contracts/generated/v4/channelsStatusValidators.mjs'
import { CHANNELS_GET_METHOD, type Params as ChannelsGetParams, type Result as ChannelsGetResult } from '@/contracts/generated/v4/channelsGet'
import { validateResult as validateChannelsGetResult } from '@/contracts/generated/v4/channelsGetValidators.mjs'
import { CHANNELS_PROBE_METHOD, type Params as ChannelsProbeParams, type Result as ChannelsProbeResult } from '@/contracts/generated/v4/channelsProbe'
import { validateResult as validateChannelsProbeResult } from '@/contracts/generated/v4/channelsProbeValidators.mjs'
import { CHANNELS_RESTART_METHOD, type Params as ChannelsRestartParams, type Result as ChannelsRestartResult } from '@/contracts/generated/v4/channelsRestart'
import { validateResult as validateChannelsRestartResult } from '@/contracts/generated/v4/channelsRestartValidators.mjs'
import { CHANNELS_LOGOUT_METHOD, type Params as ChannelsLogoutParams, type Result as ChannelsLogoutResult } from '@/contracts/generated/v4/channelsLogout'
import { validateResult as validateChannelsLogoutResult } from '@/contracts/generated/v4/channelsLogoutValidators.mjs'
import { CHANNELS_PAIRINGS_METHOD, type Params as ChannelsPairingsParams, type Result as ChannelsPairingsResult } from '@/contracts/generated/v4/channelsPairings'
import { validateResult as validateChannelsPairingsResult } from '@/contracts/generated/v4/channelsPairingsValidators.mjs'
import { CHANNELS_PAIRING_APPROVE_METHOD, type Params as ChannelsPairingApproveParams, type Result as ChannelsPairingApproveResult } from '@/contracts/generated/v4/channelsPairingApprove'
import { validateResult as validateChannelsPairingApproveResult } from '@/contracts/generated/v4/channelsPairingApproveValidators.mjs'
import { CHANNELS_PAIRING_REVOKE_METHOD, type Params as ChannelsPairingRevokeParams, type Result as ChannelsPairingRevokeResult } from '@/contracts/generated/v4/channelsPairingRevoke'
import { validateResult as validateChannelsPairingRevokeResult } from '@/contracts/generated/v4/channelsPairingRevokeValidators.mjs'
import { CHANNELS_ADMIN_SET_METHOD, type Params as ChannelsAdminSetParams, type Result as ChannelsAdminSetResult } from '@/contracts/generated/v4/channelsAdminSet'
import { validateResult as validateChannelsAdminSetResult } from '@/contracts/generated/v4/channelsAdminSetValidators.mjs'
import { CHANNEL_STATUS_EVENT } from '@/contracts/generated/v4/channelStatusEvent'
import { validatePayload as validateChannelStatusPayload } from '@/contracts/generated/v4/channelStatusEventValidators.mjs'
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

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

export function createV4ChannelAdministration(
  rpc: RpcTransport,
  events: EventTransport,
): ChannelAdministration {
  return {
    async status() {
      await rpc.ready()
      const result = await rpc.request<ChannelsStatusResult>(CHANNELS_STATUS_METHOD)
      if (!validateChannelsStatusResult(result)) throw invalid(CHANNELS_STATUS_METHOD)
      return result.channels as Channel[]
    },
    async get(name) {
      const params: ChannelsGetParams = { name }
      const result = await rpc.request<ChannelsGetResult>(CHANNELS_GET_METHOD, params)
      if (!validateChannelsGetResult(result)) throw invalid(CHANNELS_GET_METHOD)
      return result
    },
    async probe(name) {
      const params: ChannelsProbeParams = { name }
      const result = await rpc.request<ChannelsProbeResult>(CHANNELS_PROBE_METHOD, params)
      if (!validateChannelsProbeResult(result)) throw invalid(CHANNELS_PROBE_METHOD)
      return result as ProbeResult
    },
    async restart(name) {
      const params: ChannelsRestartParams = { name }
      const result = await rpc.request<ChannelsRestartResult>(CHANNELS_RESTART_METHOD, params)
      if (!validateChannelsRestartResult(result)) throw invalid(CHANNELS_RESTART_METHOD)
    },
    async logout(name) {
      const params: ChannelsLogoutParams = { name }
      const result = await rpc.request<ChannelsLogoutResult>(CHANNELS_LOGOUT_METHOD, params)
      if (!validateChannelsLogoutResult(result)) throw invalid(CHANNELS_LOGOUT_METHOD)
    },
    async listPairings(name) {
      const params: ChannelsPairingsParams = { channelName: name }
      const result = await rpc.request<ChannelsPairingsResult>(
        CHANNELS_PAIRINGS_METHOD,
        { ...params },
      )
      if (!validateChannelsPairingsResult(result)) throw invalid(CHANNELS_PAIRINGS_METHOD)
      return (result.pairings as ChannelPairing[]).filter(pairing => pairing.channelName === name)
    },
    async approvePairing(name, pairingId, asAdmin) {
      const params: ChannelsPairingApproveParams = {
        channelName: name,
        pairingId,
        ...(asAdmin ? { asAdmin: true } : {}),
      }
      const result = await rpc.request<ChannelsPairingApproveResult>(CHANNELS_PAIRING_APPROVE_METHOD, params)
      if (!validateChannelsPairingApproveResult(result)) throw invalid(CHANNELS_PAIRING_APPROVE_METHOD)
      return {
        ...(result.adminGranted !== undefined ? { adminGranted: result.adminGranted } : {}),
        ...(result.warnings !== undefined ? { warnings: result.warnings } : {}),
      } as PairingApproval
    },
    async revokePairing(name, pairingId) {
      const params: ChannelsPairingRevokeParams = { channelName: name, pairingId }
      const result = await rpc.request<ChannelsPairingRevokeResult>(CHANNELS_PAIRING_REVOKE_METHOD, params)
      if (!validateChannelsPairingRevokeResult(result)) throw invalid(CHANNELS_PAIRING_REVOKE_METHOD)
    },
    async setAdmin(name, senderId, admin) {
      const params: ChannelsAdminSetParams = { channelName: name, senderId, admin }
      const result = await rpc.request<ChannelsAdminSetResult>(
        CHANNELS_ADMIN_SET_METHOD,
        { ...params },
      )
      if (!validateChannelsAdminSetResult(result)) throw invalid(CHANNELS_ADMIN_SET_METHOD)
    },
    subscribeStatus(listener) {
      return events.subscribe(CHANNEL_STATUS_EVENT, payload => {
        if (validateChannelStatusPayload(payload)) listener()
      })
    },
  }
}
