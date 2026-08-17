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
  SandboxRuntimeAvailability,
  SandboxRuntimeComponentId,
  SandboxRuntimeComponentStatus,
  SandboxRuntimeError,
  SandboxRuntimeOperation,
  SandboxRuntimeOperationState,
  SandboxRuntimePackStatus,
  SandboxRuntimeSource,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export type SandboxPolicySection = 'files' | 'commands' | 'network' | 'runtimes'
export type { SandboxSetupOutcome } from '@/composables/sandboxSetupCoordinator'

const SECTION_SAVE_DELAY_MS = 500
const RUNTIME_STATUS_POLL_MS = 750
const RUNTIME_STATUS_RETRY_MS = 5_000
const RUNTIME_COMPONENT_IDS = ['python', 'node', 'gitBash'] as const
const RUNTIME_AVAILABILITY = new Set<SandboxRuntimeAvailability>([
  'unsupported',
  'missing',
  'ready',
  'corrupt',
])
const RUNTIME_OPERATION_STATES = new Set<SandboxRuntimeOperationState>([
  'queued',
  'downloading',
  'verifying',
  'extracting',
  'probing',
  'activating',
  'cancelling',
  'removing',
  'completed',
  'cancelled',
  'failed',
  'interrupted',
])
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function finiteNonNegative(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value
    : fallback
}

function isRuntimeComponentId(value: unknown): value is SandboxRuntimeComponentId {
  return RUNTIME_COMPONENT_IDS.some(componentId => componentId === value)
}

function normalizeRuntimeError(value: unknown): SandboxRuntimeError | null {
  if (typeof value === 'string' && value) return { message: value }
  if (!isRecord(value) || typeof value.message !== 'string' || !value.message) return null
  return {
    ...(typeof value.code === 'string' ? { code: value.code } : {}),
    message: value.message,
    ...(typeof value.retryable === 'boolean' ? { retryable: value.retryable } : {}),
    ...(value.source === 'oss' || value.source === 'github'
      ? { source: value.source as SandboxRuntimeSource }
      : {}),
  }
}

function normalizeRuntimeOperation(
  value: unknown,
  componentId: SandboxRuntimeComponentId,
): SandboxRuntimeOperation | null {
  if (!isRecord(value)) return null
  const state = typeof value.state === 'string' ? value.state : value.phase
  if (typeof state !== 'string' || !RUNTIME_OPERATION_STATES.has(
    state as SandboxRuntimeOperationState,
  )) return null
  const operationId = typeof value.operationId === 'string'
    ? value.operationId
    : typeof value.id === 'string'
      ? value.id
      : ''
  const source = value.source === 'oss' || value.source === 'github'
    ? value.source as SandboxRuntimeSource
    : null
  return {
    operationId,
    componentId,
    kind: value.kind === 'remove' ? 'remove' : 'install',
    state: state as SandboxRuntimeOperationState,
    source,
    downloadedBytes: finiteNonNegative(value.downloadedBytes),
    totalBytes: typeof value.totalBytes === 'number'
      ? finiteNonNegative(value.totalBytes)
      : null,
    progressPercent: finiteNonNegative(value.progressPercent),
    startedAtMs: finiteNonNegative(value.startedAtMs),
    updatedAtMs: finiteNonNegative(value.updatedAtMs),
    error: normalizeRuntimeError(value.error),
  }
}

function normalizeRuntimeComponent(value: unknown): SandboxRuntimeComponentStatus | null {
  if (!isRecord(value) || !isRuntimeComponentId(value.componentId)) return null
  const availability = typeof value.availability === 'string'
    && RUNTIME_AVAILABILITY.has(value.availability as SandboxRuntimeAvailability)
    ? value.availability as SandboxRuntimeAvailability
    : 'missing'
  return {
    componentId: value.componentId,
    availability,
    catalogVersion: typeof value.catalogVersion === 'string' ? value.catalogVersion : null,
    activeVersion: typeof value.activeVersion === 'string' ? value.activeVersion : null,
    installedBytes: typeof value.installedBytes === 'number'
      ? finiteNonNegative(value.installedBytes)
      : null,
    removable: value.removable === true,
    resumeAvailable: value.resumeAvailable === true,
    resumeBytes: finiteNonNegative(value.resumeBytes),
    operation: normalizeRuntimeOperation(value.operation, value.componentId),
    lastError: normalizeRuntimeError(value.lastError),
  }
}

function normalizeRuntimeStatus(value: unknown): SandboxRuntimePackStatus | null {
  if (!isRecord(value) || value.schemaVersion !== 1 || !Array.isArray(value.components)) return null
  const sourceOrder = Array.isArray(value.sourceOrder)
    ? value.sourceOrder.filter(
        (source): source is SandboxRuntimeSource => source === 'oss' || source === 'github',
      )
    : []
  const components = value.components
    .map(normalizeRuntimeComponent)
    .filter((component): component is SandboxRuntimeComponentStatus => component !== null)
  return {
    schemaVersion: 1,
    managementSupported: value.managementSupported === true,
    target: typeof value.target === 'string' ? value.target : null,
    catalogVersion: typeof value.catalogVersion === 'string' ? value.catalogVersion : null,
    sourceOrder,
    components,
    nextPollAfterMs: finiteNonNegative(value.nextPollAfterMs, RUNTIME_STATUS_POLL_MS),
  }
}

function normalizeRuntimeStatusResponse(value: unknown): SandboxRuntimePackStatus | null {
  const direct = normalizeRuntimeStatus(value)
  if (direct) return direct
  if (!isRecord(value)) return null
  return normalizeRuntimeStatus(value.status) ?? normalizeRuntimeStatus(value.runtimeStatus)
}

function isMethodNotFound(error: unknown): boolean {
  return (error as RpcClientError | null | undefined)?.code === 'METHOD_NOT_FOUND'
}

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
  let runtimeStatusRequestGeneration = 0
  let runtimeViewActive = false
  let runtimePollTimer: ReturnType<typeof setTimeout> | null = null

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
      void loadRuntimeStatus()
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
    runtimeStatusRequestGeneration += 1
    if (runtimePollTimer) clearTimeout(runtimePollTimer)
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
      await rpc.waitForConnection()
      const status = normalizeRuntimeStatusResponse(
        await rpc.call<unknown>('sandbox.runtime.status'),
      )
      if (disposed || requestGeneration !== runtimeStatusRequestGeneration) return null
      if (!status) throw new Error('Invalid runtime status response')
      runtimeStatus.value = status
      runtimeStatusSupported.value = true
      return status
    } catch (error) {
      if (disposed || requestGeneration !== runtimeStatusRequestGeneration) return null
      if (isMethodNotFound(error)) {
        runtimeStatus.value = null
        runtimeStatusSupported.value = false
        runtimeStatusError.value = ''
      } else {
        runtimeStatusError.value = errorMessage(error)
      }
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
    method: 'sandbox.runtime.install' | 'sandbox.runtime.cancel' | 'sandbox.runtime.remove',
    componentId: SandboxRuntimeComponentId,
    params: Record<string, unknown>,
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
      await rpc.waitForConnection()
      const response = await rpc.call<unknown>(method, params)
      clearRuntimePoll()
      runtimeStatusRequestGeneration += 1
      runtimeStatusLoading.value = false
      const status = normalizeRuntimeStatusResponse(response)
      if (status) {
        runtimeStatus.value = status
        runtimeStatusSupported.value = true
      } else {
        const operationPayload = isRecord(response) && isRecord(response.operation)
          ? response.operation
          : response
        const operation = normalizeRuntimeOperation(operationPayload, componentId)
        if (!operation || !applyRuntimeOperation(operation)) await loadRuntimeStatus()
      }
      scheduleRuntimePoll()
      return true
    } catch (error) {
      runtimeActionError[componentId] = errorMessage(error)
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
      'sandbox.runtime.install',
      componentId,
      { componentId },
      () => ensureRuntimeEnabled(componentId),
    )
  }

  function cancelRuntime(
    componentId: SandboxRuntimeComponentId,
    operationId: string,
  ): Promise<boolean> {
    if (!operationId) return Promise.resolve(false)
    return runRuntimeAction('sandbox.runtime.cancel', componentId, {
      componentId,
      operationId,
    })
  }

  function removeRuntime(componentId: SandboxRuntimeComponentId): Promise<boolean> {
    return runRuntimeAction('sandbox.runtime.remove', componentId, { componentId })
  }

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
    sandboxWarningSuppressed,
    desktopWarningPreferenceAvailable,
    desktopPreferencePending,
    sectionPending,
    sectionError,
    sectionDirty,
    load,
    loadRuntimeStatus,
    setRuntimeViewActive,
    enableRuntime,
    installRuntime,
    cancelRuntime,
    removeRuntime,
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
