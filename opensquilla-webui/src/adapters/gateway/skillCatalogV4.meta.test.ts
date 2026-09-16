import { describe, expect, it, vi } from 'vitest'
import type { Skill } from '@/types/skills'
import { normalizeSkill, skillLifecyclePresentation, skillStatusDotTitle } from '@/composables/skills/useSkillsCatalog'
import { createV4SkillCatalog } from './skillCatalogV4'

function setup(
  responses: Record<string, unknown>,
  methods = ['meta.list', 'meta.inspect'],
) {
  const supported = new Set(methods)
  const request = vi.fn(async (method: string) => {
    const response = responses[method]
    if (response instanceof Error) throw response
    return response
  })
  const ready = vi.fn(async () => {})
  const markUnsupported = vi.fn((method: string) => supported.delete(method))
  const catalog = createV4SkillCatalog({
    request,
    ready,
    supports: method => supported.has(method),
    markUnsupported,
  } as Parameters<typeof createV4SkillCatalog>[0])
  return { catalog, request, ready, markUnsupported, supported }
}

function meta(overrides: Record<string, unknown> = {}) {
  return {
    name: 'synthetic-meta',
    description: 'Synthetic workflow',
    layer: 'managed',
    instance_id: 'managed:synthetic-meta',
    install_id: 'synthetic-install',
    ready: true,
    status: 'ready',
    missing_bins: [],
    missing_env: [],
    missing_env_any: [],
    missing_skills: [],
    missing_capabilities: [],
    missing_provider_capabilities: [],
    reasons: [],
    setup_actions: [],
    manual_setup_actions: [],
    generation: 1,
    digest: 'synthetic-digest',
    source: 'skill://managed/synthetic-meta?generation=1',
    visibility: 'meta',
    invocation: 'meta_only',
    dependency_count: 0,
    ...overrides,
  }
}

function root(overrides: Partial<Skill> = {}): Skill {
  return {
    name: 'synthetic-meta',
    kind: 'meta',
    layer: 'managed',
    instance_id: 'managed:synthetic-meta',
    install_id: 'synthetic-install',
    active: true,
    ...overrides,
  }
}

function failure(code: string) {
  return Object.assign(new Error(`Synthetic ${code}`), { code })
}

describe('SkillCatalog Meta compatibility', () => {
  it.each([{ methods: [] }, { methods: ['meta.list'] }])('keeps old catalog reads when inspect is not advertised ($methods)', async ({ methods }) => {
    const skills = [root(), { name: 'ordinary' }]
    const { catalog, request, ready } = setup({ 'skills.list': { skills } }, methods)
    await expect(catalog.list()).resolves.toEqual(skills)
    expect(ready).toHaveBeenCalledOnce()
    expect(request).toHaveBeenCalledExactlyOnceWith('skills.list', { includeLifecycle: true }, expect.any(Object))
  })

  it('combines the exact Meta identity while preserving lifecycle and capability fields', async () => {
    const invocation = { model_catalog: false, skill_view: false, user_completion: true,
      direct_command: true, argument_substitution: true, scoped_tool_permissions: false,
      sandbox_execution: 'unknown' }
    const managed = root({ invocation, installed: true, eligible: false, missing_bins: ['uv'] })
    const other = root({ layer: 'project', instance_id: 'project:synthetic-meta', install_id: '' })
    const { catalog } = setup({
      'skills.list': { skills: [managed, other] },
      'meta.list': { skills: [meta()] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(2)
    expect(items[0]).toMatchObject({ layer: 'managed', installed: true, eligible: true, missing_bins: [], invocation })
    expect(items[1]).toEqual(other)
  })

  it('never merges different instance IDs even when the install ID and name match', async () => {
    const shadowed = root({ instance_id: 'managed:older', active: false })
    const { catalog } = setup({
      'skills.list': { skills: [shadowed] },
      'meta.list': { skills: [meta()] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(2)
    expect(items[0]).toEqual(shadowed)
    expect(items[1]?.instance_id).toBe('managed:synthetic-meta')
  })

  it('matches installation identity when one response omits the instance ID', async () => {
    const { catalog } = setup({
      'skills.list': { skills: [root()] },
      'meta.list': { skills: [meta({ instance_id: '' })] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(1)
    expect(items[0]?.instance_id).toBe('managed:synthetic-meta')
  })

  it('requires a known matching layer for legacy rows without exact identity', async () => {
    const { catalog } = setup({
      'skills.list': { skills: [root({ instance_id: '', install_id: '', layer: 'project' })] },
      'meta.list': { skills: [meta({ instance_id: '', install_id: '', layer: 'bundled' })] },
    })
    expect(await catalog.list()).toHaveLength(2)
  })

  it('does not merge different kinds by name and layer without exact identity', async () => {
    const selected = root({ kind: 'meta_sop', instance_id: '', install_id: '' })
    const { catalog } = setup({
      'skills.list': { skills: [selected] },
      'meta.list': { skills: [meta({ instance_id: '', install_id: '' })] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(2)
    expect(items[0]).toEqual(selected)
    expect(items[1]?.kind).toBe('meta')
  })

  it('merges matching kind, name, and known layer when exact identity is absent', async () => {
    const { catalog } = setup({
      'skills.list': { skills: [root({ instance_id: '', install_id: '', eligible: false })] },
      'meta.list': { skills: [meta({ instance_id: '', install_id: '' })] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: 'meta', layer: 'managed', eligible: true })
  })

  it('preserves the exact base kind and source through listing and inspection', async () => {
    const selected = root({ kind: 'meta_sop', source: 'synthetic-install-source' })
    const { catalog } = setup({
      'skills.list': { skills: [selected] },
      'meta.list': { skills: [meta()] },
      'meta.inspect': { ...meta(), dependencies: [] },
    })
    const items = await catalog.list()
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: selected.kind, source: selected.source })
    await expect(catalog.detail(items[0]!)).resolves.toMatchObject({
      kind: selected.kind, source: selected.source, eligible: true,
    })
  })

  it('keeps ordinary entries and inactive inventory when Meta discovery is disabled', async () => {
    const inactive = root({ instance_id: 'managed:disabled', active: false })
    const { catalog } = setup({
      'skills.list': { skills: [{ name: 'ordinary' }, root(), inactive] },
      'meta.list': { skills: [], disabled: true },
    })
    await expect(catalog.list()).resolves.toEqual([{ name: 'ordinary' }, inactive])
  })

  it('falls back only for a missing list method and remembers that within the connection', async () => {
    const { catalog, request, markUnsupported } = setup({
      'skills.list': { skills: [root()] },
      'meta.list': failure('METHOD_NOT_FOUND'),
    })
    expect(await catalog.list()).toEqual([root()])
    expect(markUnsupported).toHaveBeenCalledWith('meta.list')
    await catalog.list()
    expect(request.mock.calls.filter(([method]) => method === 'meta.list')).toHaveLength(1)
  })

  it.each(['UNAUTHORIZED', 'NOT_FOUND', 'INTERNAL_ERROR', 'TIMEOUT'])('does not hide list %s failures', async code => {
    const error = failure(code)
    const { catalog, markUnsupported } = setup({ 'skills.list': { skills: [] }, 'meta.list': error })
    await expect(catalog.list()).rejects.toBe(error)
    expect(markUnsupported).not.toHaveBeenCalled()
  })

  it('rejects invalid Meta list payloads instead of converting them to an empty catalog', async () => {
    const { catalog } = setup({ 'skills.list': { skills: [] }, 'meta.list': { skills: 'invalid' } })
    await expect(catalog.list()).rejects.toThrow('invalid response')
  })

  it('passes exact identity to inspect and preserves invocation capabilities', async () => {
    const selected = root({ invocation: { model_catalog: false, skill_view: false,
      user_completion: true, direct_command: true, argument_substitution: true,
      scoped_tool_permissions: false, sandbox_execution: 'unknown' } })
    const { catalog, request } = setup({
      'meta.inspect': { ...meta(), dependencies: [{ name: 'child', available: true,
        visibility: 'internal', invocation: 'meta_only', owners: ['synthetic-meta'],
        digest: 'synthetic-child-digest', source: 'skill://managed/child?generation=1' }] },
    })
    const detail = await catalog.detail(selected)
    expect(request).toHaveBeenCalledExactlyOnceWith('meta.inspect', {
      name: selected.name, instanceId: selected.instance_id, installId: selected.install_id,
    }, expect.any(Object))
    expect(detail).toMatchObject({ sub_skills: ['child'], invocation: selected.invocation, eligible: true })
  })

  it('rejects a changed winner rather than showing a different same-name installation', async () => {
    const { catalog, request } = setup({ 'meta.inspect': { ...meta({ instance_id: 'managed:replacement' }), dependencies: [] } })
    await expect(catalog.detail(root())).rejects.toThrow('identity changed')
    expect(request).toHaveBeenCalledOnce()
  })

  it('does not use old detail when Meta is disabled', async () => {
    const { catalog, request } = setup({ 'meta.inspect': { disabled: true } })
    await expect(catalog.detail(root())).rejects.toThrow('disabled')
    expect(request).toHaveBeenCalledOnce()
  })

  it('uses legacy exact detail when only meta.list is advertised', async () => {
    const selected = root({ kind: 'meta_sop' })
    const { catalog, request, markUnsupported } = setup({ 'skills.get': selected }, ['meta.list'])
    await expect(catalog.detail(selected)).resolves.toEqual(selected)
    expect(request).toHaveBeenCalledExactlyOnceWith('skills.get', {
      name: selected.name, instanceId: selected.instance_id, installId: selected.install_id, includeLifecycle: true,
    }, expect.any(Object))
    expect(markUnsupported).not.toHaveBeenCalled()
  })

  it('rejects an invalid inspection without a legacy retry', async () => {
    const { catalog, request } = setup({ 'meta.inspect': { name: 'synthetic-meta' } })
    await expect(catalog.detail(root())).rejects.toThrow('invalid response')
    expect(request).toHaveBeenCalledOnce()
  })

  it.each(['UNAUTHORIZED', 'NOT_FOUND', 'INVALID_REQUEST', 'TIMEOUT'])('does not fall back from inspect %s failures', async code => {
    const error = failure(code)
    const { catalog, request, markUnsupported } = setup({ 'meta.inspect': error })
    await expect(catalog.detail(root())).rejects.toBe(error)
    expect(request).toHaveBeenCalledOnce()
    expect(markUnsupported).not.toHaveBeenCalled()
  })

  it('preserves exact identity on unsupported inspect and resumes inspect after a new connection', async () => {
    const responses = { 'meta.inspect': failure('METHOD_NOT_FOUND') as unknown, 'skills.get': root() }
    const { catalog, request, markUnsupported, supported } = setup(responses)
    await expect(catalog.detail(root())).resolves.toEqual(root())
    expect(markUnsupported).toHaveBeenCalledWith('meta.inspect')
    expect(request).toHaveBeenLastCalledWith('skills.get', {
      name: 'synthetic-meta', instanceId: 'managed:synthetic-meta',
      installId: 'synthetic-install', includeLifecycle: true,
    }, expect.any(Object))
    supported.add('meta.inspect')
    responses['meta.inspect'] = { ...meta(), dependencies: [] }
    await expect(catalog.detail(root())).resolves.toMatchObject({ eligible: true })
    expect(request).toHaveBeenLastCalledWith('meta.inspect', expect.any(Object), expect.any(Object))
  })

  it('uses the existing exact lifecycle detail for a shadowed Meta', async () => {
    const selected = root({ active: false })
    const { catalog, request } = setup({ 'skills.get': selected })
    await expect(catalog.detail(selected)).resolves.toEqual(selected)
    expect(request).toHaveBeenCalledExactlyOnceWith('skills.get', {
      name: selected.name, instanceId: selected.instance_id, installId: selected.install_id, includeLifecycle: true,
    }, expect.any(Object))
  })

  it('clears readiness diagnostics without discarding declared Python installation metadata', async () => {
    const selected = normalizeSkill(root({
      eligible: false,
      status: 'needs_setup',
      status_detail: 'Synthetic old missing dependency',
      missing_bins: ['uv'],
      lifecycle: { install_state: 'tracked', load_state: 'loaded', selection_state: 'active',
        compatibility_state: 'native', readiness_state: 'needs_setup' },
    }))
    selected.dependency_summary!.declared.python_packages = [{ install_id: 'python-deps', label: 'Python', package: 'synthetic-pkg', module: 'synthetic' }]
    selected.dependency_summary!.sub_skill_dependencies.missing_references = ['missing-child']
    selected.install = [{ id: 'python-deps', kind: 'uv' }]
    const { catalog } = setup({ 'meta.inspect': { ...meta(), dependencies: [] } })
    const detail = normalizeSkill(await catalog.detail(selected))
    expect(detail).toMatchObject({ eligible: true, status: 'ready', missing_bins: [] })
    expect(detail.status_detail).toBe('')
    expect(skillStatusDotTitle(detail)).not.toContain('Synthetic old missing dependency')
    expect(detail.dependency_summary?.missing.count).toBe(0)
    expect(detail.dependency_summary?.sub_skill_dependencies.missing_references).toEqual([])
    expect(detail.dependency_summary?.declared.python_packages).toEqual(selected.dependency_summary!.declared.python_packages)
    expect(detail.install).toEqual(selected.install)
    expect(detail.lifecycle).toEqual({ ...selected.lifecycle, readiness_state: 'ready' })
    expect(skillLifecyclePresentation(selected, 'installed')?.tone).toBe('warning')
    expect(skillLifecyclePresentation(detail, 'installed')).toBeNull()
    expect(skillLifecyclePresentation(detail, 'registry')?.tone).toBe('success')
    expect(selected.lifecycle?.readiness_state).toBe('needs_setup')
  })

  it('replaces obsolete setup explanations with current readiness reasons', async () => {
    const { catalog } = setup({ 'meta.inspect': { ...meta({ ready: false, status: 'needs_setup',
      reasons: ['Synthetic current requirement'] }), dependencies: [] } })
    const detail = await catalog.detail(root({ status_detail: 'Synthetic old requirement' }))
    expect(detail.status_detail).toBe('Synthetic current requirement')
    expect(skillStatusDotTitle(detail)).toBe('Synthetic current requirement')
  })
})
