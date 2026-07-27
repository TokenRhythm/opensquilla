import { onUnmounted, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type { Skill, SkillDependencyInstallOutcome } from '@/types/skills'
import {
  installActionsForCurrentDependencies,
  normalizeSkill,
  skillDependencySummary,
} from '@/composables/skills/useSkillsCatalog'

interface SkillDetailRpc {
  call(method: string, params?: Record<string, unknown>): Promise<unknown>
}

interface SkillDetailControllerOptions {
  rpc: SkillDetailRpc
  installDeps: (name: string, installId: string) => Promise<SkillDependencyInstallOutcome>
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
  let closeTimerSkill = ''
  let installRequest: { generation: number; name: string } | null = null

  function clearCloseTimer() {
    if (closeTimer) clearTimeout(closeTimer)
    closeTimer = null
    closeTimerSkill = ''
  }

  function beginRequest(): number {
    requestGeneration += 1
    clearCloseTimer()
    return requestGeneration
  }

  function isCurrent(generation: number, name: string): boolean {
    return requestGeneration === generation && selectedSkill.value?.name === name
  }

  async function fetchLatest(name: string, seed: Skill): Promise<Skill> {
    const detail = await options.rpc.call('skills.get', { name }) as Skill
    // Eligible rows omit legacy missing_* fields. Clear the seed diagnostics
    // before merging so a transition to ready cannot retain stale list data.
    return normalizeSkill({
      ...seed,
      missing_bins: [],
      missing_env: [],
      missing_env_any: [],
      dependency_summary: undefined,
      ...detail,
      name,
    })
  }

  async function openSkill(skill: Skill) {
    const generation = beginRequest()
    const name = skill.name
    selectedSkill.value = normalizeSkill(skill)
    selectedSkillError.value = ''
    installFeedback.value = ''
    selectedSkillLoading.value = true
    try {
      const latest = await fetchLatest(name, skill)
      if (isCurrent(generation, name)) selectedSkill.value = latest
    } catch (error) {
      if (isCurrent(generation, name)) selectedSkillError.value = errorMessage(error)
    } finally {
      if (isCurrent(generation, name)) selectedSkillLoading.value = false
    }
  }

  function closeSkill() {
    beginRequest()
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
    const currentInstallRequest = { generation, name }
    installRequest = currentInstallRequest
    selectedSkillError.value = ''
    installFeedback.value = ''
    selectedSkillLoading.value = true

    try {
      // Installation visibility is derived from a fresh detail response rather
      // than the possibly stale list/card payload. This also prevents an old
      // dialog from invoking an action that is no longer a current dependency.
      const seed = selectedSkill.value
      const latestBeforeInstall = await fetchLatest(name, seed)
      if (!isCurrent(generation, name)) return false
      selectedSkill.value = latestBeforeInstall
      selectedSkillLoading.value = false

      const action = installActionsForCurrentDependencies(latestBeforeInstall)
        .find(item => item.id === installId)
      if (!action) {
        selectedSkillError.value = i18n.global.t('cronSkills.skillDetail.installUnavailable')
        return false
      }

      const outcome = await options.installDeps(name, installId)
      if (!isCurrent(generation, name)) return false
      if (!outcome.success) {
        installFeedback.value = outcome.message
          || i18n.global.t('cronSkills.registry.installFailed')
        return false
      }

      // The install result is useful for immediate envAny completeness, while
      // skills.get remains authoritative for the dialog and action list.
      const latestAfterInstall = await fetchLatest(name, latestBeforeInstall)
      if (!isCurrent(generation, name)) return false
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
      closeTimerSkill = name
      closeTimer = setTimeout(() => {
        if (
          requestGeneration === generation
          && closeTimerSkill === name
          && selectedSkill.value?.name === name
        ) {
          closeSkill()
        }
      }, closeDelayMs)
      return true
    } catch (error) {
      if (isCurrent(generation, name)) selectedSkillError.value = errorMessage(error)
      return false
    } finally {
      if (installRequest === currentInstallRequest) installRequest = null
      if (isCurrent(generation, name)) selectedSkillLoading.value = false
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
