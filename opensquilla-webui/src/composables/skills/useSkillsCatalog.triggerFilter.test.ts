import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { Skill } from '@/types/skills'
import { useSkillsCatalog } from './useSkillsCatalog'

// Regression (issue #1018): filtering the skills catalog crashed with
// `e.toLowerCase is not a function` when any skill's `triggers` array
// contained a non-string element (numeric YAML scalars, nested lists).
// The backend now stringifies trigger elements at parse time, and the
// filter tolerates legacy payloads (mixed-version gateways) defensively.

function makeCatalog(skills: unknown[]) {
  const catalog: Pick<SkillCatalog, 'list'> = { list: async () => skills as Skill[] }
  const options = {
    proposals: ref([]),
    autoEnabledSkills: ref([]),
    proposalsSettings: ref({
      available: false,
      enabled: false,
      on_dream_complete: false,
      auto_enable: false,
      auto_enable_max_risk: '',
    }),
    loadProposals: async () => {},
  }
  return useSkillsCatalog(catalog as SkillCatalog, options)
}

describe('useSkillsCatalog trigger filtering', () => {
  it.each(['dub', '123', 'nested'])('filters legacy mixed triggers for %s', async (query) => {
    const catalog = makeCatalog([
      {
        name: 'media-tool',
        description: 'Media processing utilities',
        triggers: [123, ['nested', 'list'], 'dubbing'],
        status: 'ready',
        layer: 'personal',
        kind: 'skill',
        eligible: true,
      },
    ])
    expect(await catalog.loadData()).toBe(true)
    catalog.filterText.value = query
    expect(() => catalog.filteredSkills.value).not.toThrow()
    // Search reaches triggers because neither the name nor description matches.
    expect(catalog.filteredSkills.value.map(s => s.name)).toEqual(['media-tool'])
  })

  it('matches string triggers case-insensitively', async () => {
    const catalog = makeCatalog([
      {
        name: 'translate',
        description: 'Translation',
        triggers: ['多语言配音', 'Dubbing Studio'],
        status: 'ready',
        layer: 'personal',
        kind: 'skill',
        eligible: true,
      },
    ])
    expect(await catalog.loadData()).toBe(true)
    catalog.filterText.value = 'dubb'
    expect(catalog.filteredSkills.value.map(s => s.name)).toEqual(['translate'])
  })
})
