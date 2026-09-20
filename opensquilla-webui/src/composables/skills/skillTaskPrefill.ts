import type { Skill, SkillCandidate } from '@/types/skills'
import { copySelectedSkills, isSelectedSkills, type SelectedSkillRef } from '@/types/selectedSkills'

export interface SkillTaskPrefill {
  prefill: string
  selectedSkillPrefill: SelectedSkillRef[]
  autosend: false
}

export function isSkillTaskEligible(skill: Skill): boolean {
  return !skill.disabled
    && skill.user_invocable !== false
    && skill.active !== false
    && (!skill.lifecycle || skill.lifecycle.selection_state === 'active')
}

/**
 * Build only a draft. Ordinary skills must resolve to the same selectable
 * candidate used by the slash palette; a managed copy must never silently
 * select a different installation with the same display name.
 */
export function prepareSkillTaskPrefill(
  skill: Skill,
  candidates: readonly SkillCandidate[],
): SkillTaskPrefill | null {
  if (!isSkillTaskEligible(skill)) return null
  if (skill.kind === 'meta' || skill.kind === 'meta_sop') {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(skill.name)) return null
    return { prefill: `/meta ${skill.name} `, selectedSkillPrefill: [], autosend: false }
  }
  const matches = candidates.filter(candidate => candidate.name === skill.name
    && (!skill.instance_id || candidate.instanceId === skill.instance_id))
  const candidate = matches.length === 1 ? matches[0] : undefined
  if (!candidate || candidate.kind !== 'skill' || candidate.disabled || !candidate.ready) return null
  return { prefill: '', selectedSkillPrefill: copySelectedSkills([candidate]), autosend: false }
}

export function readSkillTaskPrefill(state: Record<string, unknown> | null): SelectedSkillRef[] {
  const value = state?.selectedSkillPrefill
  return isSelectedSkills(value) && value.length === 1 ? copySelectedSkills(value) : []
}
