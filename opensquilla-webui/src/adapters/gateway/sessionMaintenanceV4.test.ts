import { describe, expect, it, vi } from 'vitest'
import { createV4SessionMaintenance } from './sessionMaintenanceV4'

describe('SessionMaintenance v4 adapter', () => {
  it('maps semantic commands and projects wire results', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({
        key: 'agent:main:webchat:one',
        reset: true,
        rotated: true,
        previous_session_id: 'before',
        session_id: 'after',
        epoch: 2,
      })
      .mockResolvedValueOnce({
        key: 'agent:main:webchat:one',
        compaction_id: 'cmp-one',
        status: 'started',
        compacted: false,
        applied: false,
        durability: 'none',
        user_visible: true,
      })
    const application = createV4SessionMaintenance({ request })

    await expect(application.reset({ key: 'agent:main:webchat:one' })).resolves.toEqual({
      key: 'agent:main:webchat:one',
      reset: true,
      rotated: true,
      previousSessionId: 'before',
      sessionId: 'after',
      epoch: 2,
    })
    await expect(application.compact({
      key: 'agent:main:webchat:one',
      wait: false,
    })).resolves.toMatchObject({
      compactionId: 'cmp-one',
      status: 'started',
      userVisible: true,
    })

    expect(request).toHaveBeenNthCalledWith(1, 'sessions.reset', {
      key: 'agent:main:webchat:one',
    })
    expect(request).toHaveBeenNthCalledWith(2, 'sessions.contextCompact', {
      key: 'agent:main:webchat:one',
      wait: false,
    })
  })

  it('fails closed on a malformed successful response', async () => {
    const application = createV4SessionMaintenance({
      request: vi.fn().mockResolvedValue({ status: 'started' }),
    })

    await expect(application.compact({ key: 'one' })).rejects.toThrow(
      'sessions.contextCompact returned an invalid response',
    )
  })
})
