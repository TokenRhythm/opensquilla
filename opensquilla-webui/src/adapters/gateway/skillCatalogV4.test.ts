import { describe, expect, it, vi } from 'vitest'
import { createV4SkillCatalog } from './skillCatalogV4'

function adapter(call: ReturnType<typeof vi.fn>, supports = true) {
  return createV4SkillCatalog({
    request: call,
    ready: vi.fn(async () => {}),
    supports: vi.fn((method: string) => supports && !method.startsWith('meta.')),
    markUnsupported: vi.fn(),
  } as Parameters<typeof createV4SkillCatalog>[0])
}

describe('v4 SkillCatalog Adapter', () => {
  it('loads metadata-only candidates bound to the current session', async () => {
    const result = {
      generation: 4,
      candidates: [{
        name: 'synthetic-manual', instanceId: 'workspace:synthetic', digest: 'sha256:synthetic',
        generation: 4, description: 'Synthetic description', aliases: ['示例'], kind: 'skill',
        source: 'workspace', disabled: false, manualOnly: true, ready: true,
      }],
    }
    const call = vi.fn(async () => result)
    const catalog = adapter(call)
    expect(catalog.supportsCandidates()).toBe(true)
    await expect(catalog.listCandidates({ sessionKey: 'synthetic-session' })).resolves.toEqual(result)
    expect(call).toHaveBeenCalledExactlyOnceWith('skills.candidates', {
      sessionKey: 'synthetic-session',
    }, expect.any(Object))
  })

  it('rejects a candidate source that exposes a filesystem path instead of a layer', async () => {
    const call = vi.fn(async () => ({
      generation: 4,
      candidates: [{
        name: 'synthetic-manual', instanceId: 'workspace:synthetic', digest: 'sha256:synthetic',
        generation: 4, description: 'Synthetic description', aliases: [], kind: 'skill',
        source: '/synthetic/private/skills', disabled: false, manualOnly: true, ready: true,
      }],
    }))
    await expect(adapter(call).listCandidates()).rejects.toThrow()
  })

  it('never falls back to an expensive full list on an unsupported Gateway', async () => {
    const call = vi.fn()
    const catalog = adapter(call, false)
    expect(catalog.supportsCandidates()).toBe(false)
    await expect(catalog.listCandidates()).rejects.toThrow('updated Gateway')
    expect(call).not.toHaveBeenCalled()
  })

  it('preserves persisted versus refreshed state from an atomic allow-use mutation', async () => {
    const result = { name: 'synthetic-manual', enabled: true, persisted: true, refreshed: false }
    const call = vi.fn(async () => result)
    await expect(adapter(call).setEnabled({ name: result.name, enabled: true })).resolves.toEqual(result)
    expect(call).toHaveBeenCalledExactlyOnceWith('skills.setEnabled', {
      name: result.name, enabled: true,
    }, expect.any(Object))
  })

  it('invalidates candidate readers after a persisted change and supports unsubscription', async () => {
    const call = vi.fn(async () => ({
      name: 'synthetic', enabled: true, persisted: true, refreshed: false,
    }))
    const catalog = adapter(call)
    const listener = vi.fn()
    const unsubscribe = catalog.subscribeInvalidation!(listener)
    await catalog.setEnabled({ name: 'synthetic', enabled: true })
    expect(listener).toHaveBeenCalledOnce()
    unsubscribe()
    await catalog.setEnabled({ name: 'synthetic', enabled: true })
    expect(listener).toHaveBeenCalledOnce()
  })

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
})
