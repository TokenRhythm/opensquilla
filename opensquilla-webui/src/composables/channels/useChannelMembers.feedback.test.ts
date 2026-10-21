// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import ChannelMembersPanel from '@/components/channels/ChannelMembersPanel.vue'
import type { AppSettings } from '@/modules/appSettings'
import type { ChannelAdministration, ChannelPairing } from '@/modules/channelAdministration'
import { useChannelMembers } from './useChannelMembers'

const pushToast = vi.hoisted(() => vi.fn())
const confirm = vi.hoisted(() => vi.fn(async () => true))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm }) }))

const pairing: ChannelPairing = {
  channelName: 'ops', pairingId: 'p1', senderId: 'u1', senderName: 'Alice', status: 'pending',
}

function scenario() {
  let approved = false
  let admin = false
  const administration = {
    listPairings: vi.fn(async (_name: string) => [{ ...pairing, status: approved ? 'approved' : 'pending' }]),
    approvePairing: vi.fn(async () => { approved = true; return { adminGranted: false } }),
    setAdmin: vi.fn(async () => { admin = true }),
  }
  const api = useChannelMembers({
    read: vi.fn(async () => ({ ops: admin ? ['u1'] : [] })),
  } as unknown as AppSettings, administration as unknown as ChannelAdministration)
  return { api, administration }
}

const apps: ReturnType<typeof createApp>[] = []
function mountPanel(api: ReturnType<typeof useChannelMembers>) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ChannelMembersPanel, { members: api, channelName: 'ops' }) })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  return host
}

beforeEach(() => { pushToast.mockClear(); confirm.mockClear(); i18n.global.locale.value = 'en' })
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

describe('channel member operation outcomes', () => {
  it('keeps the real approved row and admin recovery action visible alongside a failed refresh', async () => {
    const { api, administration } = scenario()
    await api.load('ops')
    const host = mountPanel(api)
    administration.listPairings.mockRejectedValueOnce(new Error('read unavailable'))
    await api.approve(pairing, true)
    await nextTick()
    expect(host.querySelector('.ch-pairing-state.is-error.is-inline')?.textContent).toContain('read unavailable')
    const row = host.querySelector('.ch-pairing-groups .ch-pairing-row')!
    expect(row.textContent).toContain('Alice')
    expect(row.querySelector('.ch-pairing-status.is-approved')).not.toBeNull()
    const adminLabel = i18n.global.t('console.channels.pairings.setAsAdminLabel', { sender: 'Alice' })
    const button = Array.from(row.querySelectorAll<HTMLButtonElement>('button')).find(item => item.getAttribute('aria-label') === adminLabel)!
    expect(button).toBeDefined()
    expect(button.disabled).toBe(false)
    button.click()
    await vi.waitFor(() => expect(administration.setAdmin).toHaveBeenCalledExactlyOnceWith('ops', 'u1', true))
    expect(administration.approvePairing).toHaveBeenCalledTimes(1)
  })

  it('keeps the original error-only state when no member data has loaded', async () => {
    const { api, administration } = scenario()
    administration.listPairings.mockRejectedValueOnce(new Error('cold read failed'))
    await api.load('ops')
    const host = mountPanel(api)
    expect(host.querySelector('.ch-pairing-state.is-error')?.textContent).toContain('cold read failed')
    expect(host.querySelector('.ch-pairing-state.is-inline')).toBeNull()
    expect(host.querySelector('.ch-pairing-groups')).toBeNull()
    expect(host.textContent).not.toContain(i18n.global.t('console.channels.pairings.emptyTitle'))
  })

  it('does not switch back to the old channel when approval finishes after navigation', async () => {
    const { api, administration } = scenario()
    await api.load('ops')
    let resolve!: (value: { adminGranted: boolean }) => void
    administration.approvePairing.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const pending = api.approve(pairing, true)
    await Promise.resolve()
    administration.listPairings.mockResolvedValueOnce([{ ...pairing, channelName: 'other', pairingId: 'other-pairing', senderId: 'other-user' }])
    await api.load('other')
    api.announcement.value = 'Other channel update'
    resolve({ adminGranted: false })
    await pending
    expect(administration.listPairings.mock.calls.map(call => call[0])).toEqual(['ops', 'other'])
    expect(api.pairings.value[0]?.pairingId).toBe('other-pairing')
    expect(api.announcement.value).toBe('Other channel update')
    expect(pushToast).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('Approved Alice, but the admin grant failed'), { tone: 'warn' })
  })

  it('reports partial success once, preserves approval, and retries only the admin grant', async () => {
    const { api, administration } = scenario()
    await api.load('ops')
    await api.approve(pairing, true)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('Approved Alice, but the admin grant failed'), { tone: 'warn' })
    expect(api.announcement.value).toBe(pushToast.mock.calls[0]?.[0])
    expect(api.approvedPairings.value).toHaveLength(1)
    expect(api.pendingPairings.value).toHaveLength(0)
    await api.setAdmin(api.approvedPairings.value[0]!, true)
    expect(administration.approvePairing).toHaveBeenCalledTimes(1)
    expect(administration.setAdmin).toHaveBeenCalledExactlyOnceWith('ops', 'u1', true)
    expect(api.isChannelAdmin('u1')).toBe(true)
    expect(pushToast).toHaveBeenCalledTimes(1)
  })

  it('does not turn an acknowledged approval back into a pending request when refresh fails', async () => {
    const { api, administration } = scenario()
    await api.load('ops')
    administration.listPairings.mockRejectedValueOnce(new Error('read unavailable'))
    await api.approve(pairing, true)
    expect(api.approvedPairings.value[0]?.status).toBe('approved')
    expect(api.pendingPairings.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(api.error.value).toContain('read unavailable')
    expect(administration.approvePairing).toHaveBeenCalledTimes(1)
  })

  it('keeps a refused approval pending and reports the original rejection inline', async () => {
    const { api, administration } = scenario()
    await api.load('ops')
    administration.approvePairing.mockRejectedValueOnce(new Error('access denied'))
    await api.approve(pairing, true)
    expect(api.pendingPairings.value).toHaveLength(1)
    expect(api.error.value).toContain('access denied')
    expect(pushToast).not.toHaveBeenCalled()
    expect(administration.setAdmin).not.toHaveBeenCalled()
  })
})
