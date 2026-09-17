import { describe, expect, it } from 'vitest'
import {
  installActionsForCurrentDependencies,
  normalizeSkill,
  skillDependencyCounts,
  skillDependencySummary,
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
      'healthy untracked bundled or meta Skill on the Installed surface',
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
      'healthy untracked bundled or meta Skill on the Community surface',
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
  it('preserves declared, OR-group, advisory, and meta-skill rollup diagnostics', () => {
    const skill = normalizeSkill({
      name: 'media-bundle',
      status: 'needs_setup',
      dependency_summary: {
        declared: {
          binaries: { all: ['ffmpeg'], any: ['node', 'bun'] },
          python_packages: [{
            install_id: 'pillow',
            label: 'Install Pillow',
            package: 'pillow',
            module: 'PIL',
          }],
          api_env: { all: ['MEDIA_TOKEN'], any: ['OPENROUTER_API_KEY', 'ARK_API_KEY'] },
        },
        missing: {
          binaries: { all: ['ffmpeg'], any: [['node', 'bun']] },
          api_env: { all: [], any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']] },
          count: 99,
        },
        inferred: {
          python_imports: [{ module: 'cv2', source: 'scripts/render.py', not_enforced: true }],
          api_env: [{ name: 'OPTIONAL_TOKEN', sources: ['SKILL.md'], not_enforced: true }],
          scan_errors: ['scripts/broken.py: syntax error'],
        },
        sub_skill_dependencies: {
          skills: [],
          missing_count: 1,
          inferred_count: 2,
          missing_references: ['missing-child'],
        },
        declaration_quality: 'partial',
      },
    })

    const summary = skillDependencySummary(skill)
    expect(summary.missing.count).toBe(3)
    expect(summary.missing.api_env.any).toEqual([['OPENROUTER_API_KEY', 'ARK_API_KEY']])
    expect(skillDependencyCounts(skill)).toEqual({
      python: 1,
      binaries: 2,
      env: 2,
      missing: 3,
      advisory: 6,
    })
  })

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

  it('only exposes install actions that match current authoritative dependencies', () => {
    const skill = normalizeSkill({
      name: 'render',
      status: 'needs_setup',
      install: [
        { id: 'ffmpeg', kind: 'brew', bins: ['ffmpeg'] },
        { id: 'stale-binary', kind: 'brew', bins: ['imagemagick'] },
        { id: 'pillow', kind: 'uv', bins: [] },
        { id: 'undeclared-package', kind: 'uv', bins: [] },
      ],
      dependency_summary: {
        declared: {
          binaries: { all: ['ffmpeg'], any: [] },
          python_packages: [{
            install_id: 'pillow',
            label: 'Pillow',
            package: 'pillow',
            module: 'PIL',
          }],
          api_env: { all: [], any: [] },
        },
        missing: {
          binaries: { all: ['ffmpeg'], any: [] },
          api_env: { all: [], any: [] },
          count: 1,
        },
        inferred: { python_imports: [], api_env: [], scan_errors: [] },
        sub_skill_dependencies: {
          skills: [], missing_count: 0, inferred_count: 0, missing_references: [],
        },
        declaration_quality: 'declared',
      },
    })

    expect(installActionsForCurrentDependencies(skill).map(action => action.id))
      .toEqual(['ffmpeg', 'pillow'])

    expect(installActionsForCurrentDependencies({ ...skill, status: 'ready' }).map(action => action.id))
      .toEqual(['ffmpeg'])
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
