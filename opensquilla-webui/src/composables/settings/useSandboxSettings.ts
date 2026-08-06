import { computed, onScopeDispose, reactive, ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { RpcClientError } from '@/lib/rpc'
import { usePlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import {
  ensureSandboxReady,
  normalizeSandboxSetupStatus,
  type SandboxSetupOutcome,
} from '@/composables/sandboxSetupCoordinator'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export type SandboxPolicySection = 'files' | 'commands' | 'network' | 'runtimes'
export type { SandboxSetupOutcome } from '@/composables/sandboxSetupCoordinator'

const SECTION_SAVE_DELAY_MS = 500

function clonePolicy(policy: SandboxPolicy): SandboxPolicy {
  return JSON.parse(JSON.stringify(policy)) as SandboxPolicy
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function currentPolicyFromConflict(error: unknown): SandboxPolicy | null {
  const rpcError = error as RpcClientError | null | undefined
  if (rpcError?.code !== 'POLICY_VERSION_CONFLICT') return null
  if (!rpcError.details || typeof rpcError.details !== 'object') return null
  const currentPolicy = (rpcError.details as { currentPolicy?: unknown }).currentPolicy
  if (!currentPolicy || typeof currentPolicy !== 'object') return null
  return clonePolicy(currentPolicy as SandboxPolicy)
}

export function useSandboxSettings() {
  const rpc = useRpcStore()
  const platform = usePlatform()
  const { pushToast } = useToasts()
  const loading = ref(false)
  const capabilityLoading = ref(false)
  const capabilityCheckFailed = ref(false)
  const sandboxSetupStatus = ref<SandboxSetupStatusPayload | null>(null)
  const sandboxSetupPending = ref(false)
  const sandboxSetupOutcome = ref<SandboxSetupOutcome>('idle')
  const loadError = ref('')
  const capability = ref<SandboxCapabilityReport | null>(null)
  const baseline = ref<SandboxPolicy | null>(null)
  const draft = ref<SandboxPolicy | null>(null)
  const builtinDenyWritePaths = ref<string[]>([])
  const runtimeTarget = ref<string | null>(null)
  const runtimeVersions = ref<SandboxPolicyDefaults['runtimeVersions']>({})
  const defaultRunModeBaseline = ref<SandboxRunMode>('full')
  const defaultRunMode = ref<SandboxRunMode>('full')
  const defaultRunModePending = ref(false)
  const defaultRunModeError = ref('')
  const sandboxWarningSuppressed = ref(false)
  const desktopWarningPreferenceAvailable = ref(false)
  const desktopPreferencePending = ref(false)
  const sectionPending = reactive<Record<SandboxPolicySection, boolean>>({
    files: false,
    commands: false,
    network: false,
    runtimes: false,
  })
  const sectionError = reactive<Record<SandboxPolicySection, string>>({
    files: '',
    commands: '',
    network: '',
    runtimes: '',
  })
  let saveQueue: Promise<void> = Promise.resolve()
  let defaultRunModeSequence = 0
  const sectionSaveTimers: Partial<Record<SandboxPolicySection, ReturnType<typeof setTimeout>>> = {}
  let disposed = false
  let capabilityRequestGeneration = 0

  const ready = computed(() => Boolean(baseline.value && draft.value))
  const canRequestSandboxSetup = computed(() => (
    platform.capabilities.isDesktop
    && capability.value?.setupSupported !== false
    && (
      sandboxSetupStatus.value?.state === 'not_setup'
      || sandboxSetupStatus.value?.state === 'failed'
    )
  ))

  function sectionDirty(section: SandboxPolicySection): boolean {
    if (!baseline.value || !draft.value) return false
    return JSON.stringify(baseline.value[section]) !== JSON.stringify(draft.value[section])
  }

  async function load(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      await rpc.waitForConnection()
      const [policyPayload, defaultsPayload, runModePayload] = await Promise.all([
        rpc.call<SandboxPolicy>('sandbox.policy.get'),
        rpc.call<Partial<SandboxPolicyDefaults>>('sandbox.policy.defaults'),
        rpc.call<{ runMode?: unknown }>('sandbox.run_mode.preference.get'),
      ])
      baseline.value = clonePolicy(policyPayload)
      draft.value = clonePolicy(policyPayload)
      builtinDenyWritePaths.value = Array.isArray(defaultsPayload.builtinDenyWritePaths)
        ? defaultsPayload.builtinDenyWritePaths.map(String)
        : []
      runtimeTarget.value = typeof defaultsPayload.runtimeTarget === 'string'
        ? defaultsPayload.runtimeTarget
        : null
      runtimeVersions.value = defaultsPayload.runtimeVersions ?? {}
      const loadedRunMode: SandboxRunMode = runModePayload.runMode === 'full' ? 'full' : 'safe'
      defaultRunModeBaseline.value = loadedRunMode
      defaultRunMode.value = loadedRunMode
      void loadSandboxReadiness()
      void loadDesktopPreference()
    } catch (error) {
      loadError.value = errorMessage(error)
    } finally {
      loading.value = false
    }
  }

  async function loadCapability(forceRefresh = false): Promise<SandboxCapabilityReport | null> {
    if (disposed) return null
    const requestGeneration = ++capabilityRequestGeneration
    capabilityLoading.value = true
    capabilityCheckFailed.value = false
    try {
      await rpc.waitForConnection()
      const report = await rpc.call<SandboxCapabilityReport>(
        'sandbox.capability.status',
        forceRefresh ? { refresh: true } : undefined,
      )
      if (disposed || requestGeneration !== capabilityRequestGeneration) return null
      capability.value = report
      return report
    } catch {
      if (disposed || requestGeneration !== capabilityRequestGeneration) return null
      capability.value = null
      capabilityCheckFailed.value = true
      return null
    } finally {
      if (!disposed && requestGeneration === capabilityRequestGeneration) {
        capabilityLoading.value = false
      }
    }
  }

  async function loadSetupStatus(): Promise<SandboxSetupStatusPayload | null> {
    if (!platform.capabilities.isDesktop || disposed) return null
    try {
      await rpc.waitForConnection()
      const status = normalizeSandboxSetupStatus(await rpc.call('sandbox.setup.status'))
      if (!disposed && status) sandboxSetupStatus.value = status
      return status
    } catch {
      // Capability status remains the visible fallback for old Gateways.
      return null
    }
  }

  async function loadSandboxReadiness(): Promise<void> {
    if (!platform.capabilities.isDesktop) {
      await loadCapability()
      return
    }
    const status = await loadSetupStatus()
    if (status === null || status.state === 'ready') await loadCapability()
  }

  async function ensureSandboxSetupForSafeMode(): Promise<boolean> {
    if (!canRequestSandboxSetup.value || sandboxSetupPending.value) return false
    sandboxSetupPending.value = true
    sandboxSetupOutcome.value = 'idle'
    try {
      const result = await ensureSandboxReady(
        (method, params) => rpc.call(method, params),
        () => loadCapability(true),
        () => rpc.waitForConnection(10_000),
      )
      if (result.status) sandboxSetupStatus.value = result.status
      sandboxSetupOutcome.value = result.outcome
      return result.ready
    } finally {
      sandboxSetupPending.value = false
    }
  }

  onScopeDispose(() => {
    disposed = true
    capabilityRequestGeneration += 1
    for (const timer of Object.values(sectionSaveTimers)) {
      if (timer) clearTimeout(timer)
    }
  })

  async function loadDesktopPreference(): Promise<void> {
    const desktop = platform.settings
    if (typeof desktop.getDesktopPreferences !== 'function') return
    desktopWarningPreferenceAvailable.value = true
    try {
      const preferences = await desktop.getDesktopPreferences()
      sandboxWarningSuppressed.value = Boolean(
        preferences.sandboxUnavailableWarningSuppressed,
      )
    } catch {
      desktopWarningPreferenceAvailable.value = false
    }
  }

  function queueSave<T>(operation: () => Promise<T>): Promise<T> {
    const queued = saveQueue.then(operation)
    saveQueue = queued.then(() => undefined, () => undefined)
    return queued
  }

  function reportSaveFailure(): void {
    pushToast(i18n.global.t('errors.saveFailed'), { tone: 'danger' })
  }

  async function setDefaultRunMode(mode: SandboxRunMode): Promise<boolean> {
    const sequence = ++defaultRunModeSequence
    const hadPendingSelection = defaultRunModePending.value
    defaultRunMode.value = mode
    defaultRunModeError.value = ''
    if (mode === defaultRunModeBaseline.value && !hadPendingSelection) return true
    defaultRunModePending.value = true
    return queueSave(async () => {
      try {
        const payload = await rpc.call<{ runMode?: unknown }>(
        'sandbox.run_mode.preference.set',
          { runMode: mode },
        )
        if (sequence === defaultRunModeSequence) {
          const savedMode: SandboxRunMode = payload.runMode === 'full' ? 'full' : 'safe'
          defaultRunModeBaseline.value = savedMode
          defaultRunMode.value = savedMode
        }
        return true
      } catch (error) {
        if (sequence === defaultRunModeSequence) {
          defaultRunModeError.value = errorMessage(error)
          defaultRunMode.value = defaultRunModeBaseline.value
          reportSaveFailure()
        }
        return false
      } finally {
        if (sequence === defaultRunModeSequence) defaultRunModePending.value = false
      }
    })
  }

  async function saveDefaultRunMode(): Promise<void> {
    await setDefaultRunMode(defaultRunMode.value)
  }

  function adoptSavedDefaultRunMode(mode: SandboxRunMode): void {
    defaultRunModeSequence += 1
    defaultRunModeBaseline.value = mode
    defaultRunMode.value = mode
    defaultRunModeError.value = ''
    defaultRunModePending.value = false
  }

  function discardDefaultRunMode(): void {
    defaultRunModeSequence += 1
    defaultRunMode.value = defaultRunModeBaseline.value
    defaultRunModeError.value = ''
  }

  async function resetSandboxUnavailableWarning(): Promise<void> {
    const desktop = platform.settings
    if (typeof desktop.saveDesktopPreferences !== 'function') return
    desktopPreferencePending.value = true
    try {
      const preferences = await desktop.saveDesktopPreferences({
        sandboxUnavailableWarningSuppressed: false,
      })
      sandboxWarningSuppressed.value = Boolean(
        preferences.sandboxUnavailableWarningSuppressed,
      )
    } finally {
      desktopPreferencePending.value = false
    }
  }

  async function performSectionSave(section: SandboxPolicySection): Promise<boolean> {
    if (!baseline.value || !draft.value || !sectionDirty(section)) return true
    sectionPending[section] = true
    sectionError[section] = ''
    const submittedBaseline = clonePolicy(baseline.value)
    const submittedSection = JSON.parse(JSON.stringify(draft.value[section]))
    try {
      const candidate = clonePolicy(submittedBaseline)
      Object.assign(candidate, { [section]: submittedSection })
      const saved = await rpc.call<SandboxPolicy>('sandbox.policy.update', {
        basePolicyVersion: submittedBaseline.policyVersion,
        policy: candidate,
      })
      const currentDraft = clonePolicy(draft.value)
      const sectionChangedWhileSaving = (
        JSON.stringify(currentDraft[section]) !== JSON.stringify(submittedSection)
      )
      baseline.value = clonePolicy(saved)
      draft.value = clonePolicy(saved)
      for (const other of ['files', 'commands', 'network', 'runtimes'] as const) {
        if (other !== section) Object.assign(draft.value, { [other]: currentDraft[other] })
      }
      if (sectionChangedWhileSaving) {
        Object.assign(draft.value, { [section]: currentDraft[section] })
        void flushSectionSave(section)
      }
      return true
    } catch (error) {
      sectionError[section] = errorMessage(error)
      const currentDraft = draft.value ? clonePolicy(draft.value) : null
      const sectionChangedWhileSaving = currentDraft !== null && (
        JSON.stringify(currentDraft[section]) !== JSON.stringify(submittedSection)
      )
      const currentPolicy = currentPolicyFromConflict(error)
      if (currentPolicy) {
        baseline.value = clonePolicy(currentPolicy)
        draft.value = clonePolicy(currentPolicy)
        if (currentDraft) {
          for (const other of ['files', 'commands', 'network', 'runtimes'] as const) {
            if (
              other !== section
              && JSON.stringify(currentDraft[other]) !== JSON.stringify(submittedBaseline[other])
            ) {
              Object.assign(draft.value, { [other]: currentDraft[other] })
            }
          }
          if (sectionChangedWhileSaving) {
            Object.assign(draft.value, { [section]: currentDraft[section] })
          }
        }
      } else if (!sectionChangedWhileSaving && baseline.value && draft.value) {
        Object.assign(draft.value, {
          [section]: JSON.parse(JSON.stringify(baseline.value[section])),
        })
      }
      reportSaveFailure()
      return false
    } finally {
      sectionPending[section] = false
    }
  }

  function clearSectionSaveTimer(section: SandboxPolicySection): void {
    const timer = sectionSaveTimers[section]
    if (timer) clearTimeout(timer)
    delete sectionSaveTimers[section]
  }

  function flushSectionSave(section: SandboxPolicySection): Promise<boolean> {
    clearSectionSaveTimer(section)
    return queueSave(() => performSectionSave(section))
  }

  function scheduleSectionSave(section: SandboxPolicySection): void {
    clearSectionSaveTimer(section)
    sectionSaveTimers[section] = setTimeout(() => {
      delete sectionSaveTimers[section]
      void flushSectionSave(section)
    }, SECTION_SAVE_DELAY_MS)
  }

  function saveSection(section: SandboxPolicySection): Promise<void> {
    return flushSectionSave(section).then(() => undefined)
  }

  function discardSection(section: SandboxPolicySection): void {
    if (!baseline.value || !draft.value) return
    clearSectionSaveTimer(section)
    Object.assign(draft.value, {
      [section]: JSON.parse(JSON.stringify(baseline.value[section])),
    })
    sectionError[section] = ''
  }

  return {
    loading,
    capabilityLoading,
    capabilityCheckFailed,
    sandboxSetupStatus,
    sandboxSetupPending,
    sandboxSetupOutcome,
    canRequestSandboxSetup,
    loadError,
    capability,
    baseline,
    draft,
    ready,
    builtinDenyWritePaths,
    runtimeTarget,
    runtimeVersions,
    defaultRunMode,
    defaultRunModeBaseline,
    defaultRunModePending,
    defaultRunModeError,
    sandboxWarningSuppressed,
    desktopWarningPreferenceAvailable,
    desktopPreferencePending,
    sectionPending,
    sectionError,
    sectionDirty,
    load,
    loadCapability,
    loadSetupStatus,
    ensureSandboxSetupForSafeMode,
    setDefaultRunMode,
    adoptSavedDefaultRunMode,
    saveDefaultRunMode,
    discardDefaultRunMode,
    resetSandboxUnavailableWarning,
    scheduleSectionSave,
    flushSectionSave,
    saveSection,
    discardSection,
  }
}
