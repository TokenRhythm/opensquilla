import { describe, expect, it } from 'vitest'
import type { SkillCandidate } from '@/types/skills'
import { prepareSkillTaskPrefill, readSkillTaskPrefill } from './skillTaskPrefill'

const candidate: SkillCandidate = {
  name: 'synthetic-skill', instanceId: 'skill:synthetic', digest: 'a'.repeat(64),
  generation: 1, description: 'Synthetic skill', aliases: [], kind: 'skill',
  source: 'bundled', disabled: false, manualOnly: false, ready: true,
}
describe('skill task prefill', () => {
  it('uses exact selected-skill references without automatically sending', () => {
    expect(prepareSkillTaskPrefill({ name: candidate.name, instance_id: candidate.instanceId }, [candidate]))
      .toEqual({ prefill: '', autosend: false, selectedSkillPrefill: [{ name: candidate.name, instanceId: candidate.instanceId, digest: candidate.digest }] })
  })
  it('rejects disabled, unavailable, ambiguous and mismatched installations', () => {
    expect(prepareSkillTaskPrefill({ name: candidate.name }, [{ ...candidate, disabled: true }])).toBeNull()
    expect(prepareSkillTaskPrefill({ name: candidate.name }, [{ ...candidate, ready: false }])).toBeNull()
    expect(prepareSkillTaskPrefill({ name: candidate.name }, [candidate, { ...candidate, instanceId: 'other' }])).toBeNull()
    expect(prepareSkillTaskPrefill({ name: candidate.name, instance_id: 'other' }, [candidate])).toBeNull()
    expect(prepareSkillTaskPrefill({ name: candidate.name, active: false }, [candidate])).toBeNull()
  })
  it('prefills the existing meta command without launching the workflow', () => {
    expect(prepareSkillTaskPrefill({ name: 'meta-synthetic', kind: 'meta', status: 'needs_setup' }, []))
      .toEqual({ prefill: '/meta meta-synthetic ', selectedSkillPrefill: [], autosend: false })
    expect(prepareSkillTaskPrefill({ name: 'meta-synthetic\n/new', kind: 'meta' }, [])).toBeNull()
    expect(prepareSkillTaskPrefill({ name: 'meta-synthetic', kind: 'meta', disabled: true }, [])).toBeNull()
  })
  it('accepts only one complete reference from route state and copies it', () => {
    const skill = { name: candidate.name, instanceId: candidate.instanceId, digest: candidate.digest }
    const state = { selectedSkillPrefill: [skill] }
    const result = readSkillTaskPrefill(state)
    expect(result).toEqual([skill])
    expect(result[0]).not.toBe(skill)
    expect(readSkillTaskPrefill({ selectedSkillPrefill: [{ name: candidate.name }] })).toEqual([])
    expect(readSkillTaskPrefill({ selectedSkillPrefill: [skill, skill] })).toEqual([])
    expect(readSkillTaskPrefill(null)).toEqual([])
  })
})
