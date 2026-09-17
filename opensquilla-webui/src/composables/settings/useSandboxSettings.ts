import { computed, inject, onScopeDispose, reactive, ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { usePlatform } from '@/platform'
import {
  SANDBOX_RUNTIME_KEY,
  SandboxError,
  type SandboxRuntimeActionReceipt,
  type SandboxSettingsRuntime,
  type SandboxSetupOutcome,
} from '@/modules/sandboxRuntime'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxRuntimeComponentId,
  SandboxRuntimeOperation,
  SandboxRuntimeOperationState,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export type SandboxPolicySection = 'files' | 'commands' | 'network' | 'runtimes'
export type { SandboxSetupOutcome } from '@/modules/sandboxRuntime'

const SECTION_SAVE_DELAY_MS = 500
const SANDBOX_STARTUP_POLL_MS = 1_000
const RUNTIME_STATUS_POLL_MS = 750
const RUNTIME_STATUS_RETRY_MS = 5_000
const ACTIVE_RUNTIME_OPERATION_STATES = new Set<SandboxRuntimeOperationState>([
  'queued',
  'downloading',
  'verifying',
  'extracting',
  'probing',
  'activating',
  'cancelling',
  'removing',
])

function hasActiveRuntimeOperation(status: SandboxRuntimePackStatus | null): boolean {
  return status?.components.some(component => (
    component.operation !== null
    && ACTIVE_RUNTIME_OPERATION_STATES.has(component.operation.state)
  )) === true
}

function clonePolicy(policy: SandboxPolicy): SandboxPolicy {
  return JSON.parse(JSON.stringify(policy)) as SandboxPolicy
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function useSandboxSettings() {
  const injectedSandbox = inject(SANDBOX_RUNTIME_KEY)
  if (!injectedSandbox) throw new Error('SandboxRuntime was not provided')
  const sandbox: SandboxSettingsRuntime = injectedSandbox
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
  const runtimeStatus = ref<SandboxRuntimePackStatus | null>(null)
  const runtimeStatusLoading = ref(false)
  const runtimeStatusSupported = ref<boolean | null>(null)
  const runtimeStatusError = ref('')
  const runtimeActionPending = reactive<Record<SandboxRuntimeComponentId, boolean>>({
    python: false,
    node: false,
    gitBash: false,
  })
  const runtimeActionError = reactive<Record<SandboxRuntimeComponentId, string>>({
    python: '',
    node: '',
    gitBash: '',
  })
  const defaultRunModeBaseline = ref<SandboxRunMode>('full')
  const defaultRunMode = ref<SandboxRunMode>('full')
  const defaultRunModePending = ref(false)
  const defaultRunModeError = ref('')
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
  let runtimeStatusRequestGeneration = 0
  let runtimeViewActive = false
  let runtimePollTimer: ReturnType<typeof setTimeout> | null = null
  let sandboxStartupPollTimer: ReturnType<typeof setTimeout> | null = null
  let sandboxStartupPending = false

  const ready = computed(() => Boolean(baseline.value && draft.value))
  const canRequestSandboxSetup = computed(() => (
    platform.capabilities.isDesktop
    && capability.value?.setupSupported !== false
    && sandboxSetupStatus.value?.state === 'not_setup'
  ))

  function sectionDirty(section: SandboxPolicySection): boolean {
    if (!baseline.value || !draft.value) return false
    return JSON.stringify(baseline.value[section]) !== JSON.stringify(draft.value[section])
  }

  async function load(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      const snapshot = await sandbox.loadSettings()
      baseline.value = clonePolicy(snapshot.policy)
      draft.value = clonePolicy(snapshot.policy)
      builtinDenyWritePaths.value = Array.isArray(snapshot.defaults.builtinDenyWritePaths)
        ? snapshot.defaults.builtinDenyWritePaths.map(String)
        : []
      runtimeTarget.value = typeof snapshot.defaults.runtimeTarget === 'string'
        ? snapshot.defaults.runtimeTarget
        : null
      runtimeVersions.value = snapshot.defaults.runtimeVersions ?? {}
      const loadedRunMode: SandboxRunMode = snapshot.preference.runMode
      defaultRunModeBaseline.value = loadedRunMode
      defaultRunMode.value = loadedRunMode
      void loadRuntimeStatus()
      void loadSandboxReadiness()
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
      const readiness = await sandbox.readiness({ refreshCapability: forceRefresh })
      if (disposed || requestGeneration !== capabilityRequestGeneration) return null
      if (platform.capabilities.isDesktop && readiness.status) {
        sandboxSetupStatus.value = readiness.status
      }
      const report = readiness.capability
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
      const readiness = await sandbox.readiness()
      const status = readiness.status
      if (!disposed && status) sandboxSetupStatus.value = status
      if (!disposed && readiness.capability) capability.value = readiness.capability
      return status
    } catch {
      // Capability status remains the visible fallback for old Gateways.
      return null
    }
  }

  async function loadSandboxReadiness(): Promise<void> {
    if (disposed) return
    if (sandboxStartupPollTimer) {
      clearTimeout(sandboxStartupPollTimer)
      sandboxStartupPollTimer = null
    }
    // The domain readiness call already combines setup state and capability.
    // Reusing its projection avoids a second capability probe for ready hosts.
    const report = await loadCapability()
    if (disposed) return
    const status = sandboxSetupStatus.value
    if (status && status.state !== 'ready') capability.value = null
    if (status !== null) sandboxStartupPending = status.state === 'setting_up'
    else if (report !== null) sandboxStartupPending = report.code === 'setting_up'
    // These reads only follow an initialization already in progress. Failed or
    // unavailable states stop polling; transport errors retain the last known
    // pending state. Reads never trigger setup or another initialization attempt.
    if (sandboxStartupPending) {
      sandboxStartupPollTimer = setTimeout(() => {
        sandboxStartupPollTimer = null
        void loadSandboxReadiness()
      }, SANDBOX_STARTUP_POLL_MS)
    }
  }

  async function ensureSandboxSetupForSafeMode(): Promise<boolean> {
    if (!canRequestSandboxSetup.value || sandboxSetupPending.value) return false
    sandboxSetupPending.value = true
    sandboxSetupOutcome.value = 'idle'
    try {
      const result = await sandbox.ensureReady()
      if (result.status) sandboxSetupStatus.value = result.status
      capability.value = result.capability
      sandboxSetupOutcome.value = result.outcome
      return result.ready
    } finally {
      sandboxSetupPending.value = false
    }
  }

  onScopeDispose(() => {
    disposed = true
    capabilityRequestGeneration += 1
    runtimeStatusRequestGeneration += 1
    if (runtimePollTimer) clearTimeout(runtimePollTimer)
    if (sandboxStartupPollTimer) clearTimeout(sandboxStartupPollTimer)
    for (const timer of Object.values(sectionSaveTimers)) {
      if (timer) clearTimeout(timer)
    }
  })

  function clearRuntimePoll(): void {
    if (runtimePollTimer) clearTimeout(runtimePollTimer)
    runtimePollTimer = null
  }

  function scheduleRuntimePoll(): void {
    clearRuntimePoll()
    const activeOperation = hasActiveRuntimeOperation(runtimeStatus.value)
    const retryStatus = Boolean(
      runtimeStatusError.value && runtimeStatusSupported.value !== false,
    )
    if (
      disposed
      || !runtimeViewActive
      || (!activeOperation && !retryStatus)
    ) return
    runtimePollTimer = setTimeout(() => {
      runtimePollTimer = null
      void loadRuntimeStatus()
    }, activeOperation ? RUNTIME_STATUS_POLL_MS : RUNTIME_STATUS_RETRY_MS)
  }

  async function loadRuntimeStatus(): Promise<SandboxRuntimePackStatus | null> {
    if (disposed || runtimeStatusSupported.value === false) return null
    clearRuntimePoll()
    const requestGeneration = ++runtimeStatusRequestGeneration
    runtimeStatusLoading.value = true
    runtimeStatusError.value = ''
    try {
      const status = await sandbox.runtimeStatus()
      if (disposed || requestGeneration !== runtimeStatusRequestGeneration) return null
      if (!status) {
        runtimeStatus.value = null
        runtimeStatusSupported.value = false
        return null
      }
      runtimeStatus.value = status
      runtimeStatusSupported.value = true
      return status
    } catch (error) {
      if (disposed || requestGeneration !== runtimeStatusRequestGeneration) return null
      runtimeStatusError.value = errorMessage(error)
      return null
    } finally {
      if (!disposed && requestGeneration === runtimeStatusRequestGeneration) {
        runtimeStatusLoading.value = false
        scheduleRuntimePoll()
      }
    }
  }

  function setRuntimeViewActive(active: boolean): void {
    runtimeViewActive = active
    clearRuntimePoll()
    if (active) void loadRuntimeStatus()
  }

  function applyRuntimeOperation(operation: SandboxRuntimeOperation): boolean {
    const status = runtimeStatus.value
    if (!status) return false
    const componentIndex = status.components.findIndex(
      component => component.componentId === operation.componentId,
    )
    if (componentIndex < 0) return false
    const components = [...status.components]
    const current = components[componentIndex]
    if (!current) return false
    components[componentIndex] = {
      ...current,
      operation,
    }
    runtimeStatus.value = { ...status, components }
    return true
  }

  async function runRuntimeAction(
    action: () => Promise<SandboxRuntimeActionReceipt>,
    componentId: SandboxRuntimeComponentId,
    actionKind: 'install' | 'cancel' | 'discard' | 'remove',
    prepare?: () => Promise<boolean>,
  ): Promise<boolean> {
    if (runtimeActionPending[componentId] || runtimeStatusSupported.value === false) return false
    runtimeActionPending[componentId] = true
    runtimeActionError[componentId] = ''
    try {
      if (prepare && !(await prepare())) {
        runtimeActionError[componentId] = i18n.global.t('errors.saveFailed')
        return false
      }
      const receipt = await action()
      clearRuntimePoll()
      runtimeStatusRequestGeneration += 1
      runtimeStatusLoading.value = false
      if (receipt.kind === 'status') {
        runtimeStatus.value = receipt.status
        runtimeStatusSupported.value = true
      } else if (!applyRuntimeOperation(receipt.operation)) {
        await loadRuntimeStatus()
      }
      scheduleRuntimePoll()
      return true
    } catch (error) {
      runtimeActionError[componentId] = errorMessage(error)
      if (actionKind === 'discard') void loadRuntimeStatus()
      return false
    } finally {
      runtimeActionPending[componentId] = false
    }
  }

  function ensureRuntimeEnabled(componentId: SandboxRuntimeComponentId): Promise<boolean> {
    if (!draft.value) return Promise.resolve(false)
    if (!draft.value.runtimes.enabled) {
      draft.value.runtimes.python = false
      draft.value.runtimes.node = false
      draft.value.runtimes.gitBash = false
    }
    draft.value.runtimes.enabled = true
    draft.value.runtimes[componentId] = true
    return flushSectionSave('runtimes')
  }

  async function enableRuntime(componentId: SandboxRuntimeComponentId): Promise<boolean> {
    if (runtimeActionPending[componentId]) return false
    runtimeActionPending[componentId] = true
    runtimeActionError[componentId] = ''
    try {
      const enabled = await ensureRuntimeEnabled(componentId)
      if (!enabled) runtimeActionError[componentId] = i18n.global.t('errors.saveFailed')
      return enabled
    } finally {
      runtimeActionPending[componentId] = false
    }
  }

  function installRuntime(componentId: SandboxRuntimeComponentId): Promise<boolean> {
    return runRuntimeAction(
      () => sandbox.installRuntime(componentId),
      componentId,
      'install',
      () => ensureRuntimeEnabled(componentId),
    )
  }

  function cancelRuntime(
    componentId: SandboxRuntimeComponentId,
    operationId: string,
  ): Promise<boolean> {
    if (!operationId) return Promise.resolve(false)
    return runRuntimeAction(
      () => sandbox.cancelRuntime(componentId, operationId),
      componentId,
      'cancel',
    )
  }

  function removeRuntime(componentId: SandboxRuntimeComponentId): Promise<boolean> {
    return runRuntimeAction(
      () => sandbox.removeRuntime(componentId),
      componentId,
      'remove',
    )
  }

  function discardRuntimeDownload(
    componentId: SandboxRuntimeComponentId,
  ): Promise<boolean> {
    return runRuntimeAction(
      () => sandbox.discardRuntimeDownload(componentId),
      componentId,
      'discard',
    )
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
        const payload = await sandbox.selectMode(mode)
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

  async function performSectionSave(section: SandboxPolicySection): Promise<boolean> {
    if (!baseline.value || !draft.value || !sectionDirty(section)) return true
    sectionPending[section] = true
    sectionError[section] = ''
    const submittedBaseline = clonePolicy(baseline.value)
    const submittedSection = JSON.parse(JSON.stringify(draft.value[section]))
    try {
      const candidate = clonePolicy(submittedBaseline)
      Object.assign(candidate, { [section]: submittedSection })
      const saved = await sandbox.updatePolicy(submittedBaseline.policyVersion, candidate)
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
      const currentPolicy = error instanceof SandboxError && error.code === 'conflict'
        ? error.currentPolicy ?? null
        : null
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
    runtimeStatus,
    runtimeStatusLoading,
    runtimeStatusSupported,
    runtimeStatusError,
    runtimeActionPending,
    runtimeActionError,
    defaultRunMode,
    defaultRunModeBaseline,
    defaultRunModePending,
    defaultRunModeError,
    sectionPending,
    sectionError,
    sectionDirty,
    load,
    loadRuntimeStatus,
    setRuntimeViewActive,
    enableRuntime,
    installRuntime,
    cancelRuntime,
    discardRuntimeDownload,
    removeRuntime,
    loadCapability,
    loadSetupStatus,
    ensureSandboxSetupForSafeMode,
    setDefaultRunMode,
    adoptSavedDefaultRunMode,
    saveDefaultRunMode,
    discardDefaultRunMode,
    scheduleSectionSave,
    flushSectionSave,
    saveSection,
    discardSection,
  }
}
