import { describe, expect, it } from 'vitest'
import {
  installActionsForCurrentDependencies,
  normalizeSkill,
  skillCatalogKey,
  skillLifecyclePresentation,
} from './useSkillsCatalog'

describe('skill catalog identity', () => {
  it('keeps same-name layer and install candidates on distinct Vue keys', () => {
    expect(skillCatalogKey({ name: 'shared', layer: 'bundled', instance_id: 'bundled:1' }))
      .toBe('instance:bundled:1')
    expect(skillCatalogKey({ name: 'shared', layer: 'managed', instance_id: 'managed:2' }))
      .toBe('instance:managed:2')
    expect(skillCatalogKey({ name: 'shared', layer: 'managed', install_id: 'install-3' }))
      .toBe('install:install-3')
    expect(skillCatalogKey({ name: 'shared', layer: 'bundled' }))
      .not.toBe(skillCatalogKey({ name: 'shared', layer: 'workspace' }))
  })
})

describe('skill lifecycle presentations', () => {
  it.each([
    [
      'healthy tracked installation on the Installed surface',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      null,
    ],
    [
      'healthy tracked installation on the Community surface',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'registry',
      { label: 'Active', tone: 'success' },
    ],
    [
      'healthy untracked bundled Skill on the Installed surface',
      {
        install_state: 'untracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'native',
        readiness_state: 'ready',
      },
      'installed',
      null,
    ],
    [
      'healthy untracked bundled Skill on the Community surface',
      {
        install_state: 'untracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'native',
        readiness_state: 'ready',
      },
      'registry',
      null,
    ],
    [
      'installation requiring setup',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'degraded',
        readiness_state: 'needs_setup',
      },
      'installed',
      { label: 'Setup required', tone: 'warning' },
    ],
    [
      'active installation with limited compatibility',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'degraded',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Limited compatibility', tone: 'warning' },
    ],
    [
      'shadowed installation',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'shadowed',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Shadowed', tone: 'neutral' },
    ],
    [
      'disabled installation',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'disabled',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Disabled', tone: 'neutral' },
    ],
    [
      'model-hidden installation',
      {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'hidden',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Hidden from model catalog', tone: 'neutral' },
    ],
    [
      'offline validation',
      {
        install_state: 'tracked',
        load_state: 'validated_offline',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Validated for next start', tone: 'info' },
    ],
    [
      'loader rejection',
      {
        install_state: 'tracked',
        load_state: 'rejected',
        selection_state: 'shadowed',
        compatibility_state: 'instruction_only',
        readiness_state: 'unknown',
      },
      'installed',
      { label: 'Rejected as incompatible', tone: 'danger' },
    ],
    [
      'unsupported dialect',
      {
        install_state: 'tracked',
        load_state: 'not_discovered',
        selection_state: 'shadowed',
        compatibility_state: 'unsupported',
        readiness_state: 'unknown',
      },
      'installed',
      { label: 'Rejected as incompatible', tone: 'danger' },
    ],
    [
      'restored previous version',
      {
        install_state: 'tracked',
        load_state: 'serving_previous',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Previous version restored', tone: 'warning' },
    ],
    [
      'locally modified installation',
      {
        install_state: 'drifted',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'instruction_only',
        readiness_state: 'ready',
      },
      'installed',
      { label: 'Local changes detected', tone: 'warning' },
    ],
    [
      'missing installation files',
      {
        install_state: 'missing',
        load_state: 'not_discovered',
        selection_state: 'shadowed',
        compatibility_state: 'unsupported',
        readiness_state: 'unknown',
      },
      'installed',
      { label: 'Installed files missing', tone: 'danger' },
    ],
    [
      'otherwise undiscovered candidate',
      {
        install_state: 'tracked',
        load_state: 'not_discovered',
        selection_state: 'active',
        compatibility_state: 'native',
        readiness_state: 'unknown',
      },
      'registry',
      null,
    ],
  ] as const)('renders the %s lifecycle', (_case, lifecycle, surface, expected) => {
    expect(skillLifecyclePresentation({ name: 'community-skill', lifecycle }, surface))
      .toEqual(expected)
  })

  it('reports a missing tree ahead of rejection and a shadowed selection state', () => {
    expect(skillLifecyclePresentation({
      name: 'missing-skill',
      lifecycle: {
        install_state: 'missing',
        load_state: 'rejected',
        selection_state: 'shadowed',
        compatibility_state: 'unsupported',
        readiness_state: 'unknown',
      },
    }, 'registry')).toEqual({ label: 'Installed files missing', tone: 'danger' })
  })

  it('reports a loader rejection ahead of a shadowed selection state', () => {
    expect(skillLifecyclePresentation({
      name: 'rejected-skill',
      lifecycle: {
        install_state: 'tracked',
        load_state: 'rejected',
        selection_state: 'shadowed',
        compatibility_state: 'instruction_only',
        readiness_state: 'unknown',
      },
    }, 'registry')).toEqual({ label: 'Rejected as incompatible', tone: 'danger' })
  })

  it('reports setup ahead of limited compatibility', () => {
    expect(skillLifecyclePresentation({
      name: 'setup-skill',
      lifecycle: {
        install_state: 'tracked',
        load_state: 'loaded',
        selection_state: 'active',
        compatibility_state: 'degraded',
        readiness_state: 'needs_setup',
      },
    }, 'registry')).toEqual({ label: 'Setup required', tone: 'warning' })
  })
})

describe('skill dependency summary normalization', () => {

  it('backfills an old Gateway payload including envAny', () => {
    const skill = normalizeSkill({
      name: 'legacy-detail',
      status: 'needs_setup',
      missing_bins: ['ffmpeg'],
      missing_env: ['MEDIA_TOKEN'],
      missing_env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
    })

    expect(skill.dependency_summary?.declared).toEqual({
      binaries: { all: ['ffmpeg'], any: [] },
      python_packages: [],
      api_env: {
        all: ['MEDIA_TOKEN'],
        any: ['OPENROUTER_API_KEY', 'ARK_API_KEY'],
      },
    })
    expect(skill.dependency_summary?.missing.count).toBe(3)
  })

  it.each([
    ['shadowed', 'loaded'],
    ['disabled', 'loaded'],
    ['hidden', 'loaded'],
    ['active', 'not_discovered'],
  ] as const)('hides name-based dependency actions for a %s/%s candidate', (
    selectionState,
    loadState,
  ) => {
    const candidate = normalizeSkill({
      name: 'shared',
      active: false,
      status: 'needs_setup',
      missing_bins: ['ffmpeg'],
      install: [{ id: 'ffmpeg', kind: 'brew', bins: ['ffmpeg'] }],
      lifecycle: {
        install_state: 'tracked',
        load_state: loadState,
        selection_state: selectionState,
        compatibility_state: 'instruction_only',
        readiness_state: 'needs_setup',
      },
    })

    expect(installActionsForCurrentDependencies(candidate)).toEqual([])
  })
})
