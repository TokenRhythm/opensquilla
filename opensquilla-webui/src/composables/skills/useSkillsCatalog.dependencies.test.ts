import { describe, expect, it } from 'vitest'
import {
  installActionsForCurrentDependencies,
  normalizeSkill,
  skillDependencyCounts,
  skillDependencySummary,
} from './useSkillsCatalog'

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
})
