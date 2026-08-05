import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import { useSkillsCatalog } from './useSkillsCatalog'

// Regression (issue #1018): filtering the skills catalog crashed with
// `e.toLowerCase is not a function` when any skill's `triggers` array
// contained a non-string element (numeric YAML scalars, nested lists).
// The backend now stringifies trigger elements at parse time, and the
// filter tolerates legacy payloads (mixed-version gateways) defensively.

function makeCatalog(skills: unknown[]) {
  const rpc = {
    waitForConnection: async () => {},
    call: async () => ({ skills }),
  }
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
  return useSkillsCatalog(rpc as never, options)
}

describe('useSkillsCatalog trigger filtering', () => {
  it('does not crash when a skill trigger element is not a string', async () => {
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
    await catalog.loadData()
    catalog.filterText.value = 'dub'
    expect(() => catalog.filteredSkills.value).not.toThrow()
    // The string trigger still matches even though name/description do not.
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
    await catalog.loadData()
    catalog.filterText.value = 'dubb'
    expect(catalog.filteredSkills.value.map(s => s.name)).toEqual(['translate'])
  })
})
