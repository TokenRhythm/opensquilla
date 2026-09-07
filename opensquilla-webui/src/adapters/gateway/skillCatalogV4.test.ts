import { describe, expect, it, vi } from 'vitest'
import { createV4SkillCatalog } from './skillCatalogV4'

function adapter(call: ReturnType<typeof vi.fn>, supports = true) {
  return createV4SkillCatalog({
    request: call,
    ready: vi.fn(async () => {}),
    supports: vi.fn(() => supports),
  } as Parameters<typeof createV4SkillCatalog>[0])
}

describe('v4 SkillCatalog Adapter', () => {
  it.each([
    [{}, {}],
    [{ name: '', installId: '' }, {}],
    [{ name: 'synthetic-skill' }, { name: 'synthetic-skill' }],
    [{ installId: 'synthetic-install' }, { installId: 'synthetic-install' }],
    [
      { name: 'synthetic-skill', installId: 'synthetic-install' },
      { name: 'synthetic-skill', installId: 'synthetic-install' },
    ],
  ])('preserves uninstall parameters and Gateway rejection (%#)', async (request, expected) => {
    const rejection = new Error('synthetic Gateway rejection')
    const call = vi.fn().mockRejectedValue(rejection)

    await expect(adapter(call).uninstall(request)).rejects.toBe(rejection)
    expect(call).toHaveBeenCalledExactlyOnceWith('skills.uninstall', expected, expect.any(Object))
  })

  it('maps catalog reads and exact lifecycle identity', async () => {
    const call = vi.fn(async (method: string) => (
      method === 'skills.list'
        ? { skills: [{ name: 'managed-skill', instance_id: 'managed:1' }] }
        : { name: 'managed-skill', content: '# managed' }
    ))
    const catalog = adapter(call)

    await expect(catalog.list()).resolves.toEqual([{ name: 'managed-skill', instance_id: 'managed:1' }])
    await catalog.detail({ name: 'managed-skill', instance_id: 'managed:1', install_id: 'install-1' })

    expect(call).toHaveBeenLastCalledWith('skills.get', {
      name: 'managed-skill',
      includeLifecycle: true,
      instanceId: 'managed:1',
      installId: 'install-1',
    }, expect.any(Object))
  })

  it('keeps operation identity and risk acknowledgement inside install semantics', async () => {
    const call = vi.fn(async () => ({ success: true, installed: true }))
    const catalog = adapter(call)

    await catalog.install({
      identifier: '@acme/demo',
      source: 'clawhub',
      operationId: 'operation-1',
      riskConfirmation: 'confirmation-token',
    })

    expect(call).toHaveBeenCalledWith('skills.install', {
      identifier: '@acme/demo',
      source: 'clawhub',
      operationId: 'operation-1',
      force: true,
      riskConfirmation: 'confirmation-token',
    }, expect.any(Object))
  })

  it('combines proposal projections without making one optional read fatal', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'exec.proposals.list') return { proposals: [{ proposal_id: 'deadbeef' }] }
      if (method === 'exec.proposals.auto_enabled.list') throw new Error('legacy gateway')
      return {
        available: true,
        enabled: false,
        on_dream_complete: false,
        auto_enable: false,
        auto_enable_max_risk: 'low',
        cron: '0 5 * * *',
        window_days: 30,
        min_freq: 3,
        top_k: 5,
      }
    })
    const catalog = adapter(call)

    await expect(catalog.proposals()).resolves.toMatchObject({
      proposals: [{ proposal_id: 'deadbeef' }],
      autoEnabledSkills: [],
      settings: { available: true, enabled: false },
    })
  })
})
