import { ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import type { RegistryResult, SkillDependencyInstallOutcome } from '@/types/skills'

interface RegistrySearchData {
  results?: RegistryResult[]
}

interface InstallResult {
  success: boolean
  name?: string
  message?: string
  missing_still?: {
    bins?: string[]
    env?: string[]
    env_any?: string[][]
  }
}

export interface SkillRegistry {
  registryQuery: Ref<string>
  githubUrl: Ref<string>
  registryResults: Ref<RegistryResult[]>
  registryLoading: Ref<boolean>
  installingId: Ref<string | null>
  installingDepsId: Ref<string | null>
  uninstallingName: Ref<string | null>
  searchRegistry: () => Promise<void>
  installGithub: () => void
  installSkill: (identifier: string, source: string) => Promise<void>
  installDeps: (name: string, installId: string) => Promise<SkillDependencyInstallOutcome>
  uninstallSkill: (name: string) => Promise<boolean>
}

export function useSkillRegistry(
  rpc: ReturnType<typeof useRpcStore>,
  loadData: () => Promise<boolean>,
): SkillRegistry {
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const registryQuery = ref('')
  const githubUrl = ref('')
  const registryResults = ref<RegistryResult[]>([])
  const registryLoading = ref(false)
  const installingId = ref<string | null>(null)
  const installingDepsId = ref<string | null>(null)
  const uninstallingName = ref<string | null>(null)

  async function searchRegistry() {
    if (!registryQuery.value.trim()) return
    registryLoading.value = true
    registryResults.value = []
    try {
      const data = await rpc.call<RegistrySearchData>('skills.search', { query: registryQuery.value.trim(), limit: 20 })
      registryResults.value = data.results || []
    } catch (err) {
      pushToast(t('cronSkills.registry.toastSearchFailed', { error: (err as Error).message }), { tone: 'danger' })
    } finally {
      registryLoading.value = false
    }
  }

  function installGithub() {
    const url = githubUrl.value.trim()
    if (!url) return
    void installSkill(url, 'github')
  }

  function markRegistryResultInstalled(identifier: string, source: string, installedName?: string) {
    const installSource = source || 'clawhub'
    registryResults.value = registryResults.value.map((result) => {
      const resultSource = result.source || 'clawhub'
      const resultIdentifier = result.identifier || result.name
      const sameSource = resultSource === installSource
      const sameIdentifier =
        resultIdentifier === identifier ||
        result.identifier === identifier ||
        result.name === identifier
      const sameInstalledName = !!installedName && result.name === installedName

      if (!sameSource || (!sameIdentifier && !sameInstalledName)) return result
      return { ...result, installed: true }
    })
  }

  async function installSkill(identifier: string, source: string) {
    installingId.value = identifier
    try {
      const res = await rpc.call<InstallResult>('skills.install', { identifier, source })
      if (res.success) {
        markRegistryResultInstalled(identifier, source, res.name)
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
      } else {
        pushToast(res.message || t('cronSkills.registry.installFailed'), { tone: 'danger' })
      }
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
    } finally {
      installingId.value = null
    }
  }

  async function installDeps(name: string, installId: string): Promise<SkillDependencyInstallOutcome> {
    const failed = (message = ''): SkillDependencyInstallOutcome => ({
      success: false,
      complete: false,
      message,
      missingStill: { bins: [], env: [], env_any: [] },
    })
    if (!name || !installId) return failed()
    installingDepsId.value = installId
    try {
      const res = await rpc.call<InstallResult>('skills.deps.install', { name, install_id: installId })
      if (res.success) {
        pushToast(res.message || t('cronSkills.registry.installed'), { tone: 'ok' })
        const still = res.missing_still || {}
        const missingStill = {
          bins: still.bins || [],
          env: still.env || [],
          env_any: still.env_any || [],
        }
        const stillMissing = missingStill.bins.length
          + missingStill.env.length
          + missingStill.env_any.length
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return {
          success: true,
          complete: stillMissing === 0,
          message: res.message || '',
          missingStill,
        }
      }
      pushToast(res.message || t('cronSkills.registry.installFailed'), { tone: 'danger' })
      return failed(res.message || '')
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return failed((err as Error).message)
    } finally {
      installingDepsId.value = null
    }
  }

  async function uninstallSkill(name: string): Promise<boolean> {
    uninstallingName.value = name
    try {
      const res = await rpc.call<InstallResult>('skills.uninstall', { name })
      if (res.success) {
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return true
      }
      pushToast(res.message || t('cronSkills.registry.uninstallFailed'), { tone: 'danger' })
      return false
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return false
    } finally {
      uninstallingName.value = null
    }
  }

  return {
    registryQuery,
    githubUrl,
    registryResults,
    registryLoading,
    installingId,
    installingDepsId,
    uninstallingName,
    searchRegistry,
    installGithub,
    installSkill,
    installDeps,
    uninstallSkill,
  }
}
