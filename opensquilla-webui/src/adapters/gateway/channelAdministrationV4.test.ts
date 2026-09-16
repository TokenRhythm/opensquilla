import { describe, expect, it, vi } from 'vitest'
import { createV4ChannelAdministration } from './channelAdministrationV4'

describe('ChannelAdministration v4 Adapter', () => {
  it('projects status, saved configuration, and channel-scoped pairings', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'channels.status') return { channels: [{ name: 'ops', status: 'connected' }] }
      if (method === 'channels.get') return { entry: { name: 'ops', token: '***' }, secretFields: ['token'] }
      if (method === 'channels.pairings') {
        return { pairings: [
          { pairingId: 'one', channelName: 'ops', senderId: 'u1', status: 'pending' },
          { pairingId: 'two', channelName: 'other', senderId: 'u2', status: 'pending' },
        ] }
      }
      return {}
    })
    const administration = createV4ChannelAdministration(
      { request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )

    await expect(administration.status()).resolves.toEqual([{ name: 'ops', status: 'connected' }])
    await expect(administration.get('ops')).resolves.toEqual({
      entry: { name: 'ops', token: '***' },
      secretFields: ['token'],
    })
    await expect(administration.listPairings('ops')).resolves.toEqual([
      { pairingId: 'one', channelName: 'ops', senderId: 'u1', status: 'pending' },
    ])
  })

  it('maps pairing and runtime operations to their exact v4 inputs', async () => {
    const pairing = {
      pairingId: 'p1', channelName: 'ops', senderId: 'u1', status: 'approved',
    }
    const request = vi.fn(async (method: string) => {
      if (method === 'channels.pairing.approve') return { pairing, adminGranted: true }
      if (method === 'channels.admin.set') {
        return { channelName: 'ops', senderId: 'u1', admin: false, admins: [] }
      }
      if (method === 'channels.restart') return { status: 'restarted', channel: 'ops' }
      return {}
    })
    const administration = createV4ChannelAdministration(
      { request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )

    await administration.approvePairing('ops', 'p1', true)
    expect(request).toHaveBeenLastCalledWith('channels.pairing.approve', {
      channelName: 'ops', pairingId: 'p1', asAdmin: true,
    })
    await administration.setAdmin('ops', 'u1', false)
    expect(request).toHaveBeenLastCalledWith('channels.admin.set', {
      channelName: 'ops', senderId: 'u1', admin: false,
    })
    await administration.restart('ops')
    expect(request).toHaveBeenLastCalledWith('channels.restart', { name: 'ops' })
  })

  it('validates status events before notifying consumers', () => {
    const subscribe = vi.fn((_event: string, handler: (payload: unknown) => void) => {
      handler({ name: 'ops', status: 'running' })
      handler({ name: 'ops' })
      return { close: vi.fn() }
    })
    const listener = vi.fn()
    const administration = createV4ChannelAdministration(
      { request: vi.fn() as never, ready: vi.fn(async () => undefined) },
      { subscribe },
    )

    administration.subscribeStatus(listener)

    expect(subscribe).toHaveBeenCalledWith('channel.status', expect.any(Function))
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
