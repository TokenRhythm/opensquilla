import { computed, ref, type ComputedRef } from 'vue'

export type SkillMutationOwner =
  | 'install_queue'
  | 'dependency_install'
  | 'uninstall'
  | 'reload'
  | 'proposal'

export interface SkillMutationGate {
  owner: ComputedRef<SkillMutationOwner | null>
  busy: ComputedRef<boolean>
  acquire: (owner: SkillMutationOwner) => boolean
  release: (owner: SkillMutationOwner) => void
}

/** Serializes all Skill mutations owned by one Skills surface. */
export function createSkillMutationGate(): SkillMutationGate {
  const currentOwner = ref<SkillMutationOwner | null>(null)

  function acquire(owner: SkillMutationOwner): boolean {
    if (currentOwner.value !== null) return false
    currentOwner.value = owner
    return true
  }

  function release(owner: SkillMutationOwner) {
    if (currentOwner.value === owner) currentOwner.value = null
  }

  return {
    owner: computed(() => currentOwner.value),
    busy: computed(() => currentOwner.value !== null),
    acquire,
    release,
  }
}
