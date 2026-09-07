import { describe, expect, it, vi } from 'vitest'
import { createV4ChannelSetup } from './channelSetupV4'

describe('ChannelSetup v4 Adapter', () => {
  it('scrubs redaction sentinels before draft probe and upsert', async () => {
    const request = vi.fn(async (method: string) => (
      method === 'onboarding.channel.upsert'
        ? { changed: true, restartRequired: false, entry: { name: 'ops' } }
        : { status: 'validated' }
    ))
    const setup = createV4ChannelSetup({ request: request as never })
    const entry = { type: 'slack', name: 'ops', token: '***', note: 'keep' }

    await setup.probeDraft(entry)
    expect(request).toHaveBeenLastCalledWith('onboarding.channel.probe', {
      entry: { type: 'slack', name: 'ops', note: 'keep' },
    })
    await expect(setup.upsert(entry)).resolves.toEqual({
      name: 'ops', changed: true, restartRequired: false, liveApplyFailed: false,
    })
    expect(request).toHaveBeenLastCalledWith('onboarding.channel.upsert', {
      entry: { type: 'slack', name: 'ops', note: 'keep' },
    })
  })

  it('uses only real onboarding channel methods for lifecycle changes', async () => {
    const request = vi.fn(async () => ({ changed: true, restartRequired: true }))
    const setup = createV4ChannelSetup({ request: request as never })

    await setup.setEnabled('ops', true)
    expect(request).toHaveBeenLastCalledWith('onboarding.channel.enable', { name: 'ops' })
    await setup.setEnabled('ops', false)
    expect(request).toHaveBeenLastCalledWith('onboarding.channel.disable', { name: 'ops' })
    await setup.remove('ops')
    expect(request).toHaveBeenLastCalledWith('onboarding.channel.remove', { name: 'ops' })
  })
})
