import type { InjectionKey } from 'vue'

import type { Skill } from '@/types/skills'

/** Typed MetaSkill reads exposed to Vue without leaking Gateway method names. */
export interface MetaSkillCatalog {
  list(): Promise<Skill[]>
  inspect(name: string): Promise<Skill>
}

export const META_SKILL_CATALOG_KEY: InjectionKey<MetaSkillCatalog> =
  Symbol('MetaSkillCatalog')

export const unavailableMetaSkillCatalog: MetaSkillCatalog = {
  async list() {
    return []
  },
  async inspect() {
    throw new Error('MetaSkill catalog is unavailable.')
  },
}
