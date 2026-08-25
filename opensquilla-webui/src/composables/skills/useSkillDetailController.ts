import { onUnmounted, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type { Skill, SkillDependencyInstallOutcome } from '@/types/skills'
import {
  installActionsForCurrentDependencies,
  normalizeSkill,
  skillCatalogKey,
  skillDependencySummary,
} from '@/composables/skills/useSkillsCatalog'

interface SkillDetailRpc {
  call(method: string, params?: Record<string, unknown>): Promise<unknown>
}

interface SkillDetailControllerOptions {
  rpc: SkillDetailRpc
  installDeps: (
    name: string,
    installId: string,
    skillInstallId?: string,
    instanceId?: string,
  ) => Promise<SkillDependencyInstallOutcome>
  closeDelayMs?: number
}

export interface SkillDetailController {
  selectedSkill: Ref<Skill | null>
  selectedSkillLoading: Ref<boolean>
  selectedSkillError: Ref<string>
  installFeedback: Ref<string>
  openSkill: (skill: Skill) => Promise<void>
  closeSkill: () => void
  installCurrentDependencies: (name: string, installId: string) => Promise<boolean>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function useSkillDetailController(
  options: SkillDetailControllerOptions,
): SkillDetailController {
  const selectedSkill = ref<Skill | null>(null)
  const selectedSkillLoading = ref(false)
  const selectedSkillError = ref('')
  const installFeedback = ref('')
  const closeDelayMs = options.closeDelayMs ?? 600
  let requestGeneration = 0
  let closeTimer: ReturnType<typeof setTimeout> | null = null
  let closeTimerIdentity = ''
  let installRequest: { generation: number; identity: string } | null = null
  let activeIdentity = ''

  function clearCloseTimer() {
    if (closeTimer) clearTimeout(closeTimer)
    closeTimer = null
    closeTimerIdentity = ''
  }

  function beginRequest(): number {
    requestGeneration += 1
    clearCloseTimer()
    return requestGeneration
  }

  function isCurrent(generation: number, identity: string): boolean {
    return requestGeneration === generation && activeIdentity === identity
  }

  async function fetchLatest(seed: Skill): Promise<Skill> {
    const params: Record<string, unknown> = {
      name: seed.name,
      includeLifecycle: true,
    }
    if (seed.instance_id) params.instanceId = seed.instance_id
    if (seed.install_id) params.installId = seed.install_id
    const detail = await options.rpc.call('skills.get', {
      ...params,
    }) as Skill
    // Eligible rows omit legacy missing_* fields. Clear the seed diagnostics
    // before merging so a transition to ready cannot retain stale list data.
    return normalizeSkill({
      ...seed,
      missing_bins: [],
      missing_env: [],
      missing_env_any: [],
      dependency_summary: undefined,
      ...detail,
      name: seed.name,
    })
  }

  async function openSkill(skill: Skill) {
    const generation = beginRequest()
    const identity = skillCatalogKey(skill)
    activeIdentity = identity
    selectedSkill.value = normalizeSkill(skill)
    selectedSkillError.value = ''
    installFeedback.value = ''
    selectedSkillLoading.value = true
    try {
      const latest = await fetchLatest(skill)
      if (isCurrent(generation, identity)) selectedSkill.value = latest
    } catch (error) {
      if (isCurrent(generation, identity)) selectedSkillError.value = errorMessage(error)
    } finally {
      if (isCurrent(generation, identity)) selectedSkillLoading.value = false
    }
  }

  function closeSkill() {
    beginRequest()
    activeIdentity = ''
    selectedSkill.value = null
    selectedSkillLoading.value = false
    selectedSkillError.value = ''
    installFeedback.value = ''
  }

  async function installCurrentDependencies(name: string, installId: string): Promise<boolean> {
    if (
      !name
      || !installId
      || selectedSkill.value?.name !== name
      || installRequest?.generation === requestGeneration
    ) {
      return false
    }

    const generation = beginRequest()
    const identity = skillCatalogKey(selectedSkill.value)
    activeIdentity = identity
    const currentInstallRequest = { generation, identity }
    installRequest = currentInstallRequest
    selectedSkillError.value = ''
    installFeedback.value = ''
    selectedSkillLoading.value = true

    try {
      // Installation visibility is derived from a fresh detail response rather
      // than the possibly stale list/card payload. This also prevents an old
      // dialog from invoking an action that is no longer a current dependency.
      const seed = selectedSkill.value
      const latestBeforeInstall = await fetchLatest(seed)
      if (!isCurrent(generation, identity)) return false
      selectedSkill.value = latestBeforeInstall
      selectedSkillLoading.value = false

      const action = installActionsForCurrentDependencies(latestBeforeInstall)
        .find(item => item.id === installId)
      if (!action) {
        selectedSkillError.value = i18n.global.t('cronSkills.skillDetail.installUnavailable')
        return false
      }

      const outcome = await options.installDeps(
        name,
        installId,
        latestBeforeInstall.install_id || '',
        latestBeforeInstall.instance_id || '',
      )
      if (!isCurrent(generation, identity)) return false
      if (!outcome.success) {
        installFeedback.value = outcome.message
          || i18n.global.t('cronSkills.registry.installFailed')
        return false
      }

      // The install result is useful for immediate envAny completeness, while
      // skills.get remains authoritative for the dialog and action list.
      const latestAfterInstall = await fetchLatest(latestBeforeInstall)
      if (!isCurrent(generation, identity)) return false
      selectedSkill.value = latestAfterInstall

      const missingCount = skillDependencySummary(latestAfterInstall).missing.count
      const complete = outcome.complete
        && missingCount === 0
        && latestAfterInstall.status !== 'needs_setup'
      if (!complete) {
        const remaining = Math.max(
          missingCount,
          outcome.missingStill.bins.length
            + outcome.missingStill.env.length
            + outcome.missingStill.env_any.length,
        )
        installFeedback.value = i18n.global.t(
          'cronSkills.skillDetail.installIncomplete',
          { count: remaining },
        )
        return false
      }

      installFeedback.value = i18n.global.t('cronSkills.skillDetail.installComplete')
      closeTimerIdentity = identity
      closeTimer = setTimeout(() => {
        if (
          requestGeneration === generation
          && closeTimerIdentity === identity
          && selectedSkill.value !== null
          && skillCatalogKey(selectedSkill.value) === identity
        ) {
          closeSkill()
        }
      }, closeDelayMs)
      return true
    } catch (error) {
      if (isCurrent(generation, identity)) selectedSkillError.value = errorMessage(error)
      return false
    } finally {
      if (installRequest === currentInstallRequest) installRequest = null
      if (isCurrent(generation, identity)) selectedSkillLoading.value = false
    }
  }

  onUnmounted(() => {
    requestGeneration += 1
    clearCloseTimer()
  })

  return {
    selectedSkill,
    selectedSkillLoading,
    selectedSkillError,
    installFeedback,
    openSkill,
    closeSkill,
    installCurrentDependencies,
  }
}
