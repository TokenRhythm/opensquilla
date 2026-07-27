import { computed, ref } from 'vue'
import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import { useConfirm } from '@/composables/useConfirm'
import { approvePairingParams, errorMessage, withPendingKey } from '@/composables/channels/shared'

// Members state for one selected channel: pairing requests, approved access,
// and channel-admin standing. Owned by the /channels view (state survives tab
// switches) and rendered by ChannelMembersPanel. Members mutations commit
// live — deliberately unlike the configuration editor's draft/save model.

export interface ChannelPairing {
  pairingId: string
  pairingCode?: string
  channelName: string
  senderId: string
  senderName?: string | null
  status: 'pending' | 'approved' | string
  createdAt?: string | null
  approvedAt?: string | null
}

interface PairingsResponse {
  pairings?: ChannelPairing[]
}

/**
 * First-pairing admin bootstrap rule: the "approve as admin" checkbox may
 * default on only when the channel is KNOWN to have no approved members and
 * no admins yet. A null count means unknown (a failed fetch) and must never
 * arm the default — fail closed.
 */
export function bootstrapAsAdminDefault(
  approvedCount: number | null,
  adminCount: number | null,
): boolean {
  return approvedCount === 0 && adminCount === 0
}

export function useChannelMembers() {
  const rpc = useRpcStore()
  const { pushToast } = useToasts()
  const { confirm } = useConfirm()
  const t = i18n.global.t

  const activeName = ref('')
  const pairings = ref<ChannelPairing[]>([])
  const search = ref('')
  const loading = ref(false)
  const error = ref('')
  const announcement = ref('')
  let requestId = 0
  // Channel admins for the selected channel (channel_admin_senders[name]).
  // Members can chat + use safe tools; admins get the full tool surface.
  // Refetched with every pairing load so a grant/revoke reflects immediately.
  const adminSenders = ref<string[]>([])
  // KNOWN-empty vs unknown: a failed config read leaves the admin list
  // unknown, and the first-pairing bootstrap must never arm on unknown.
  const adminsKnown = ref(false)
  // Per-pairing "approve as admin" checkbox state; an entry here is an
  // explicit operator choice that overrides the first-pairing bootstrap.
  const adminOverrides = ref<Record<string, boolean>>({})
  const pendingActions = ref(new Set<string>())

  function reset(): void {
    requestId += 1
    activeName.value = ''
    pairings.value = []
    search.value = ''
    loading.value = false
    error.value = ''
    announcement.value = ''
    adminSenders.value = []
    adminsKnown.value = false
    adminOverrides.value = {}
  }

  function matchesSearch(pairing: ChannelPairing): boolean {
    const query = search.value.trim().toLowerCase()
    if (!query) return true
    return [pairing.senderName, pairing.senderId, pairing.pairingCode]
      .some(value => String(value || '').toLowerCase().includes(query))
  }

  const pendingPairings = computed(() =>
    pairings.value.filter(pairing => pairing.status === 'pending' && matchesSearch(pairing)))
  const approvedPairings = computed(() =>
    pairings.value.filter(pairing => pairing.status === 'approved' && matchesSearch(pairing)))
  const revokedPairings = computed(() =>
    pairings.value.filter(pairing => pairing.status === 'revoked' && matchesSearch(pairing)))
  // Unfiltered pending count drives the tab badge (a filtered-out request
  // still awaits the operator).
  const pendingCount = computed(() =>
    pairings.value.filter(pairing => pairing.status === 'pending').length)

  function isChannelAdmin(senderId?: string | null): boolean {
    return Boolean(senderId) && adminSenders.value.includes(String(senderId))
  }

  // Admins configured directly (added to channel_admin_senders in TOML) who
  // have no approved pairing row — surfaced so the members list is complete
  // and the grant is still removable from the UI.
  const adminOnlySenders = computed(() => {
    const approvedIds = new Set(
      pairings.value.filter(pairing => pairing.status === 'approved').map(pairing => pairing.senderId),
    )
    const query = search.value.trim().toLowerCase()
    return adminSenders.value.filter(id =>
      !approvedIds.has(id) && (!query || id.toLowerCase().includes(query)))
  })

  // First-pairing bootstrap: default the "as admin" checkbox on only when the
  // channel is KNOWN to have no approved members and no admins yet, so the
  // very first person approved becomes an admin unless the operator opts out.
  // An unknown admin list (failed config read) must never arm the default.
  const noApprovedOrAdmins = computed(() => bootstrapAsAdminDefault(
    pairings.value.filter(pairing => pairing.status === 'approved').length,
    adminsKnown.value ? adminSenders.value.length : null,
  ))

  // The bootstrap default is anchored to the UNFILTERED first pending request:
  // a search query must not move the pre-checked default onto whichever row
  // happens to match first.
  const firstPendingId = computed(() =>
    pairings.value.find(pairing => pairing.status === 'pending')?.pairingId ?? '')

  function isBootstrapPairing(pairing: ChannelPairing): boolean {
    return pairing.pairingId === firstPendingId.value && noApprovedOrAdmins.value
  }
  function asAdminChecked(pairing: ChannelPairing): boolean {
    if (pairing.pairingId in adminOverrides.value) return adminOverrides.value[pairing.pairingId]
    return pairing.pairingId === firstPendingId.value && noApprovedOrAdmins.value
  }

  function setAsAdminChecked(pairing: ChannelPairing, value: boolean): void {
    adminOverrides.value = { ...adminOverrides.value, [pairing.pairingId]: value }
  }

  function actionKey(pairing: ChannelPairing, action: string): string {
    return `pairing:${activeName.value}:${pairing.pairingId}:${action}`
  }

  function actionPending(pairing: ChannelPairing, action: string): boolean {
    return pendingActions.value.has(actionKey(pairing, action))
  }

  // Bounded config read: fetch only channel_admin_senders (config.get takes a
  // dot path, so we never pull the whole config into this view) and keep just
  // the list for the selected channel.
  async function loadChannelAdmins(name: string, id: number): Promise<void> {
    try {
      const map = await rpc.call<Record<string, unknown> | null>('config.get', {
        path: 'channel_admin_senders',
      })
      if (activeName.value !== name || id !== requestId) return
      const list = map && typeof map === 'object' ? (map as Record<string, unknown>)[name] : undefined
      adminSenders.value = Array.isArray(list) ? list.map(String) : []
      adminsKnown.value = true
    } catch {
      // Admin standing is supplementary; a failed fetch leaves members
      // visible without admin pills rather than breaking the whole view —
      // but the list stays UNKNOWN, not known-empty.
      if (activeName.value === name && id === requestId) {
        adminSenders.value = []
        adminsKnown.value = false
      }
    }
  }

  async function load(name: string): Promise<void> {
    const id = ++requestId
    activeName.value = name
    loading.value = true
    error.value = ''
    try {
      // Cold-load guard: a deep-linked Members tab can fire before the WS
      // handshake completes; wait instead of hard-failing with a Retry.
      await rpc.waitForConnection()
      const result = await rpc.call<PairingsResponse>('channels.pairings', { channelName: name })
      if (activeName.value !== name || id !== requestId) return
      pairings.value = (result.pairings || []).filter(pairing => pairing.channelName === name)
      await loadChannelAdmins(name, id)
    } catch (err) {
      if (activeName.value === name && id === requestId) {
        error.value = t('console.channels.pairings.loadFailed', { error: errorMessage(err) })
      }
    } finally {
      if (activeName.value === name && id === requestId) loading.value = false
    }
  }

  async function approve(pairing: ChannelPairing, asAdmin = false): Promise<void> {
    const name = activeName.value
    const sender = pairing.senderName || pairing.senderId
    const confirmed = await confirm({
      title: t('console.channels.pairings.approveConfirmTitle'),
      body: asAdmin
        ? t('console.channels.pairings.approveAdminConfirmBody', { sender, channel: name })
        : t('console.channels.pairings.approveConfirmBody', { sender, channel: name }),
      primaryLabel: t('console.channels.pairings.approve'),
      primaryClass: 'btn--primary',
    })
    if (!confirmed) return
    await withPendingKey(pendingActions, actionKey(pairing, pairing.status === 'revoked' ? 'reapprove' : 'approve'), async () => {
      error.value = ''
      try {
        const res = await rpc.call<{ adminGranted?: boolean; warnings?: string[] }>(
          'channels.pairing.approve', approvePairingParams(name, pairing.pairingId, asAdmin))
        // The backend commits the approval even when the admin grant fails
        // (adminGranted:false + warnings) — surface that instead of falsely
        // announcing an admin success.
        if (asAdmin && res?.adminGranted === false) {
          announcement.value = t('console.channels.pairings.approveSuccess', { sender })
          pushToast(announcement.value, { tone: 'ok' })
          pushToast(
            t('console.channels.pairings.adminGrantFailedAfterApprove', { sender }),
            { tone: 'danger' },
          )
        } else {
          announcement.value = asAdmin
            ? t('console.channels.pairings.approveAdminSuccess', { sender })
            : t('console.channels.pairings.approveSuccess', { sender })
          pushToast(announcement.value, { tone: 'ok' })
        }
        await load(name)
      } catch (err) {
        error.value = t('console.channels.pairings.approveFailed', { sender, error: errorMessage(err) })
      }
    })
  }

  async function setAdmin(pairing: ChannelPairing, admin: boolean): Promise<void> {
    const name = activeName.value
    const sender = pairing.senderName || pairing.senderId
    const confirmed = await confirm({
      title: admin
        ? t('console.channels.pairings.setAsAdminConfirmTitle')
        : t('console.channels.pairings.removeAdminConfirmTitle'),
      body: admin
        ? t('console.channels.pairings.setAsAdminConfirmBody', { sender, channel: name })
        : t('console.channels.pairings.removeAdminConfirmBody', { sender, channel: name }),
      primaryLabel: admin
        ? t('console.channels.pairings.setAsAdmin')
        : t('console.channels.pairings.removeAdmin'),
      primaryClass: admin ? 'btn--primary' : undefined,
    })
    if (!confirmed) return
    await withPendingKey(pendingActions, actionKey(pairing, 'admin'), async () => {
      error.value = ''
      try {
        await rpc.call('channels.admin.set', {
          channelName: name,
          senderId: pairing.senderId,
          admin,
        })
        announcement.value = admin
          ? t('console.channels.pairings.adminGrantedSuccess', { sender })
          : t('console.channels.pairings.adminRemovedSuccess', { sender })
        pushToast(announcement.value, { tone: 'ok' })
        await load(name)
      } catch (err) {
        error.value = t('console.channels.pairings.adminUpdateFailed', { sender, error: errorMessage(err) })
      }
    })
  }

  function adminOnlyKey(senderId: string): string {
    return `admin:${activeName.value}:${senderId}`
  }

  function adminOnlyActionPending(senderId: string): boolean {
    return pendingActions.value.has(adminOnlyKey(senderId))
  }

  async function removeAdminOnly(senderId: string): Promise<void> {
    const name = activeName.value
    const confirmed = await confirm({
      title: t('console.channels.pairings.removeAdminConfirmTitle'),
      body: t('console.channels.pairings.removeAdminConfirmBody', { sender: senderId, channel: name }),
      primaryLabel: t('console.channels.pairings.removeAdmin'),
    })
    if (!confirmed) return
    await withPendingKey(pendingActions, adminOnlyKey(senderId), async () => {
      error.value = ''
      try {
        await rpc.call('channels.admin.set', { channelName: name, senderId, admin: false })
        announcement.value = t('console.channels.pairings.adminRemovedSuccess', { sender: senderId })
        pushToast(announcement.value, { tone: 'ok' })
        await load(name)
      } catch (err) {
        error.value = t('console.channels.pairings.adminUpdateFailed', { sender: senderId, error: errorMessage(err) })
      }
    })
  }

  async function revoke(pairing: ChannelPairing): Promise<void> {
    const name = activeName.value
    const sender = pairing.senderName || pairing.senderId
    const confirmed = await confirm({
      title: t('console.channels.pairings.revokeConfirmTitle'),
      body: t('console.channels.pairings.revokeConfirmBody', { sender, channel: name }),
      primaryLabel: t('console.channels.pairings.revoke'),
    })
    if (!confirmed) return
    await withPendingKey(pendingActions, actionKey(pairing, 'revoke'), async () => {
      error.value = ''
      try {
        await rpc.call('channels.pairing.revoke', { channelName: name, pairingId: pairing.pairingId })
        announcement.value = t('console.channels.pairings.revokeSuccess', { sender })
        pushToast(announcement.value, { tone: 'ok' })
        await load(name)
      } catch (err) {
        error.value = t('console.channels.pairings.revokeFailed', { sender, error: errorMessage(err) })
      }
    })
  }

  return {
    pairings,
    search,
    loading,
    error,
    announcement,
    adminSenders,
    adminsKnown,
    pendingPairings,
    approvedPairings,
    revokedPairings,
    adminOnlySenders,
    pendingCount,
    noApprovedOrAdmins,
    isChannelAdmin,
    asAdminChecked,
    isBootstrapPairing,
    setAsAdminChecked,
    actionPending,
    adminOnlyActionPending,
    reset,
    load,
    approve,
    setAdmin,
    removeAdminOnly,
    revoke,
  }
}

export type ChannelMembersApi = ReturnType<typeof useChannelMembers>
