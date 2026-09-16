import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EffectScope } from 'vue'
import type {
  SandboxRuntimeActionReceipt,
  SandboxSettingsRuntime,
} from '@/modules/sandboxRuntime'
import type {
  SandboxPolicy,
  SandboxRuntimeOperation,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

const policy: SandboxPolicy = {
  schemaVersion: 2,
  policyVersion: 0,
  files: {
    customDenyWritePaths: [],
    recursiveDeleteBackupEnabled: true,
    backupQuotaBytes: 3 * 1024 ** 3,
  },
  commands: {
    requireApprovalPrefixes: [],
    autoAllowPrefixes: [],
    systemTools: 'prompt',
  },
  network: {
    blockAllNetwork: false,
    allowDomains: [],
    denyDomains: [],
  },
  runtimes: {
    enabled: true,
    python: true,
    node: true,
    gitBash: true,
  },
}

const unavailableReport = {
  available: false,
  backend: 'windows_default',
  platform: 'win32',
  code: 'probe_timeout',
  reason: 'timed out',
  setupSupported: true,
  restartRequired: false,
  probeVersion: 1,
  capabilities: [],
}

const readyRuntimeStatus: SandboxRuntimePackStatus = {
  schemaVersion: 1,
  managementSupported: true,
  target: 'windows-x64',
  catalogVersion: '2026-08-21.2',
  sourceOrder: ['oss', 'github'],
  components: [
    {
      componentId: 'python',
      availability: 'ready',
      catalogVersion: '2026-08-21.2',
      activeVersion: '3.13.15+20260814',
      installedBytes: 100,
      removable: true,
      resumeAvailable: false,
      resumeBytes: 0,
      operation: null,
      lastError: null,
    },
  ],
  nextPollAfterMs: 750,
}

async function settle(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

async function createSandboxSettings(options: {
  desktop?: boolean
  capabilityError?: boolean
  capabilityResult?: unknown
  setupState?: SandboxSetupStatusPayload['state']
  setupStatus?: () => SandboxSetupStatusPayload
  policyUpdate?: (params: Record<string, unknown>) => unknown | Promise<unknown>
  policyConflict?: SandboxPolicy
  policyConflictGate?: Promise<unknown>
  runModeSetError?: Error
  runtimeStatus?: SandboxRuntimePackStatus | null | (() => SandboxRuntimePackStatus | null | Promise<SandboxRuntimePackStatus | null>)
  runtimeStatusError?: Error
  runtimeAction?: (
    action: 'install' | 'cancel' | 'discard' | 'remove',
    componentId: string,
    operationId?: string,
  ) => SandboxRuntimeActionReceipt
} = {}) {
  vi.resetModules()
  const pushToast = vi.fn()
  const { SandboxError } = await import('@/modules/sandboxRuntime')
  const setupStatus = vi.fn(async () => {
    if (options.setupStatus) return options.setupStatus()
    const state = options.setupState ?? 'not_setup'
    return {
      state,
      platform: 'win32',
      message: state === 'ready' ? 'ready' : 'setup required',
      requiresAdmin: state !== 'ready',
    } satisfies SandboxSetupStatusPayload
  })
  const capability = vi.fn(async (_refreshCapability = false) => {
    if (options.capabilityError) throw new Error('probe failed')
    return await (options.capabilityResult ?? unavailableReport) as typeof unavailableReport
  })
  const loadSettings = vi.fn(async () => ({
    policy: structuredClone(policy),
    defaults: {
        runtimeTarget: 'windows-x64',
        runtimeVersions: { python: { version: '3.13.14', available: true } },
    },
    preference: { runMode: 'full' as const },
  }))
  const readiness = vi.fn(async (request?: { refreshCapability?: boolean }) => {
    const status = await setupStatus()
    const report = status.state === 'ready' || options.desktop !== true
      ? await capability(request?.refreshCapability === true)
      : null
    return { status, capability: report }
  })
  const ensureReady = vi.fn(async () => {
    const status: SandboxSetupStatusPayload = {
        state: 'ready',
        platform: 'win32',
        message: 'ready',
        requiresAdmin: false,
    }
    const report = await capability(true)
    return report.available
      ? { ready: true, status, capability: report, outcome: 'ready' as const }
      : { ready: false, status, capability: report, outcome: 'verification_failed' as const }
  })
  let pendingPolicyConflict = options.policyConflict
  const updatePolicy = vi.fn(async (basePolicyVersion: number, candidate: SandboxPolicy) => {
    if (pendingPolicyConflict) {
      if (options.policyConflictGate) await options.policyConflictGate
      const currentPolicy = pendingPolicyConflict
      pendingPolicyConflict = undefined
      throw new SandboxError('conflict', 'policy version conflict', {
        currentPolicy: structuredClone(currentPolicy),
        retryable: true,
      })
    }
    if (options.policyUpdate) {
      return await options.policyUpdate({ basePolicyVersion, policy: candidate }) as SandboxPolicy
    }
    const saved = structuredClone(candidate)
    saved.policyVersion = basePolicyVersion + 1
    return saved
  })
  const selectMode = vi.fn(async (mode: 'safe' | 'full') => {
    if (options.runModeSetError) throw options.runModeSetError
    return { runMode: mode, source: 'preference' }
  })
  const runtimeStatus = vi.fn(async () => {
    if (options.runtimeStatusError) throw options.runtimeStatusError
    if (typeof options.runtimeStatus === 'function') return await options.runtimeStatus()
    return options.runtimeStatus ?? null
  })
  const runtimeAction = (
    action: 'install' | 'cancel' | 'discard' | 'remove',
    componentId: string,
    operationId?: string,
  ) => options.runtimeAction?.(action, componentId, operationId)
    ?? { kind: 'status' as const, status: structuredClone(readyRuntimeStatus) }
  const installRuntime = vi.fn(async (componentId: 'python' | 'node' | 'gitBash') => (
    runtimeAction('install', componentId)
  ))
  const cancelRuntime = vi.fn(async (
    componentId: 'python' | 'node' | 'gitBash',
    operationId: string,
  ) => runtimeAction('cancel', componentId, operationId))
  const discardRuntimeDownload = vi.fn(async (componentId: 'python' | 'node' | 'gitBash') => (
    runtimeAction('discard', componentId)
  ))
  const removeRuntime = vi.fn(async (componentId: 'python' | 'node' | 'gitBash') => (
    runtimeAction('remove', componentId)
  ))
  const sandbox: SandboxSettingsRuntime = {
    readiness,
    ensureReady,
    loadSettings,
    updatePolicy,
    preference: vi.fn(async () => ({ runMode: 'full' as const, source: 'preference' })),
    selectMode,
    onPreferenceChanged: vi.fn(() => () => undefined),
    runtimeStatus,
    installRuntime,
    cancelRuntime,
    removeRuntime,
    discardRuntimeDownload,
  }
  vi.doMock('@/platform', () => ({
    usePlatform: () => ({
      capabilities: { isDesktop: options.desktop === true },
      settings: {},
    }),
  }))
  vi.doMock('@/composables/useToasts', () => ({
    useToasts: () => ({ pushToast }),
  }))

  const { createApp, effectScope, h } = await import('vue')
  const { SANDBOX_RUNTIME_KEY } = await import('@/modules/sandboxRuntime')
  const { useSandboxSettings } = await import('./useSandboxSettings')
  const app = createApp({ render: () => h('div') })
  app.provide(SANDBOX_RUNTIME_KEY, sandbox as never)
  const scope: EffectScope = effectScope()
  const settings = app.runWithContext(() => scope.run(() => useSandboxSettings()))!
  return {
    operations: {
      readiness,
      ensureReady,
      loadSettings,
      updatePolicy,
      selectMode,
      runtimeStatus,
      installRuntime,
      cancelRuntime,
      discardRuntimeDownload,
      removeRuntime,
    },
    pushToast,
    scope,
    settings,
  }
}

afterEach(() => {
  vi.doUnmock('@/platform')
  vi.doUnmock('@/composables/useToasts')
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useSandboxSettings auto-save', () => {
  it.each(['failed', 'unavailable', 'setting_up'] as const)(
    'does not offer first-time setup for %s', async setupState => {
      const { operations, scope, settings } = await createSandboxSettings({ desktop: true, setupState })
      await settings.load()
      await settle()
      expect(settings.sandboxSetupStatus.value?.state).toBe(setupState)
      expect(settings.canRequestSandboxSetup.value).toBe(false)
      expect(await settings.ensureSandboxSetupForSafeMode()).toBe(false)
      expect(operations.ensureReady).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('persists a default mode selection without a separate save action', async () => {
    const { operations, scope, settings } = await createSandboxSettings()
    await settings.load()

    await settings.setDefaultRunMode('safe')

    expect(operations.selectMode).toHaveBeenCalledWith('safe')
    expect(settings.defaultRunMode.value).toBe('safe')
    expect(settings.defaultRunModeBaseline.value).toBe('safe')
    scope.stop()
  })

  it('adopts a mode already persisted by the shared setup task without writing it twice', async () => {
    const { operations, scope, settings } = await createSandboxSettings()
    await settings.load()

    settings.adoptSavedDefaultRunMode('safe')

    expect(settings.defaultRunMode.value).toBe('safe')
    expect(settings.defaultRunModeBaseline.value).toBe('safe')
    expect(operations.selectMode).not.toHaveBeenCalled()
    scope.stop()
  })

  it('debounces free-form section edits for 500 milliseconds', async () => {
    vi.useFakeTimers()
    const { operations, scope, settings } = await createSandboxSettings()
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true

    settings.scheduleSectionSave('network')
    await vi.advanceTimersByTimeAsync(499)
    expect(operations.updatePolicy).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    await settle()
    expect(operations.updatePolicy).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        network: expect.objectContaining({ blockAllNetwork: true }),
      }),
    )
    scope.stop()
  })

  it('rolls back only the failed section and shows one toast', async () => {
    const { pushToast, scope, settings } = await createSandboxSettings({
      policyUpdate: async () => { throw new Error('save rejected') },
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true
    settings.draft.value!.files.customDenyWritePaths.push('D:\\keep-this-draft')

    await expect(settings.flushSectionSave('network')).resolves.toBe(false)

    expect(settings.draft.value!.network.blockAllNetwork).toBe(false)
    expect(settings.draft.value!.files.customDenyWritePaths).toEqual(['D:\\keep-this-draft'])
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
    scope.stop()
  })

  it('preserves edits made to the same section during an in-flight save', async () => {
    let resolveFirst!: (value: unknown) => void
    const first = new Promise(resolve => { resolveFirst = resolve })
    let updateCount = 0
    const { operations, scope, settings } = await createSandboxSettings({
      policyUpdate: async (params) => {
        updateCount += 1
        if (updateCount === 1) return first
        const saved = structuredClone(params.policy as typeof policy)
        saved.policyVersion = Number(params.basePolicyVersion) + 1
        return saved
      },
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true
    const saving = settings.flushSectionSave('network')
    await settle()

    settings.draft.value!.network.denyDomains.push('telemetry.example.com')
    const firstSaved = structuredClone(policy)
    firstSaved.policyVersion = 1
    firstSaved.network.blockAllNetwork = true
    resolveFirst(firstSaved)

    await expect(saving).resolves.toBe(true)
    await settle()

    expect(settings.draft.value!.network.denyDomains).toEqual(['telemetry.example.com'])
    expect(operations.updatePolicy).toHaveBeenCalledTimes(2)
    expect(operations.updatePolicy.mock.calls[1]).toEqual([
      1,
      expect.objectContaining({
          network: expect.objectContaining({
            denyDomains: ['telemetry.example.com'],
          }),
      }),
    ])
    scope.stop()
  })

  it('preserves newer same-section edits when an in-flight save fails', async () => {
    let rejectFirst!: (reason?: unknown) => void
    const first = new Promise((_resolve, reject) => { rejectFirst = reject })
    const { scope, settings } = await createSandboxSettings({
      policyUpdate: async () => first,
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true
    const saving = settings.flushSectionSave('network')
    await settle()

    settings.draft.value!.network.denyDomains.push('telemetry.example.com')
    rejectFirst(new Error('save rejected'))

    await expect(saving).resolves.toBe(false)
    expect(settings.draft.value!.network).toEqual({
      blockAllNetwork: true,
      allowDomains: [],
      denyDomains: ['telemetry.example.com'],
    })
    scope.stop()
  })

  it('adopts the current policy after a version conflict and can save again', async () => {
    const currentPolicy = structuredClone(policy)
    currentPolicy.policyVersion = 1
    currentPolicy.network.denyDomains = ['desktop.example.com']
    const { operations, scope, settings } = await createSandboxSettings({
      policyConflict: currentPolicy,
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true

    await expect(settings.flushSectionSave('network')).resolves.toBe(false)

    expect(settings.baseline.value).toEqual(currentPolicy)
    expect(settings.draft.value).toEqual(currentPolicy)

    settings.draft.value!.network.blockAllNetwork = true
    await expect(settings.flushSectionSave('network')).resolves.toBe(true)

    expect(operations.updatePolicy).toHaveBeenCalledTimes(2)
    expect(operations.updatePolicy.mock.calls[1]).toEqual([
      1,
      expect.objectContaining({
        network: expect.objectContaining({
          blockAllNetwork: true,
          denyDomains: ['desktop.example.com'],
        }),
      }),
    ])
    scope.stop()
  })

  it('keeps concurrent local drafts while adopting a conflict baseline', async () => {
    let releaseConflict!: () => void
    const first = new Promise<void>((resolve) => { releaseConflict = resolve })
    const currentPolicy = structuredClone(policy)
    currentPolicy.policyVersion = 1
    currentPolicy.network.denyDomains = ['desktop.example.com']
    const { scope, settings } = await createSandboxSettings({
      policyConflict: currentPolicy,
      policyConflictGate: first,
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true
    const saving = settings.flushSectionSave('network')
    await settle()

    settings.draft.value!.network.denyDomains.push('web.example.com')
    settings.draft.value!.files.customDenyWritePaths.push('/keep-local')
    releaseConflict()

    await expect(saving).resolves.toBe(false)
    expect(settings.baseline.value).toEqual(currentPolicy)
    expect(settings.draft.value!.network).toEqual({
      blockAllNetwork: true,
      allowDomains: [],
      denyDomains: ['web.example.com'],
    })
    expect(settings.draft.value!.files.customDenyWritePaths).toEqual(['/keep-local'])
    scope.stop()
  })
})

describe('useSandboxSettings capability checks', () => {
  it('loads ready desktop setup and capability with one domain read', async () => {
    const { operations, scope, settings } = await createSandboxSettings({
      desktop: true,
      setupState: 'ready',
      capabilityResult: {
        ...unavailableReport,
        available: true,
        code: 'ready',
      },
    })

    await settings.load()
    await settle()

    expect(settings.sandboxSetupStatus.value?.state).toBe('ready')
    expect(settings.capability.value?.available).toBe(true)
    expect(operations.readiness).toHaveBeenCalledOnce()
    scope.stop()
  })

  it.each(['ready', 'failed'] as const)(
    'updates a pending startup to %s without reopening settings or retrying initialization',
    async (finalState) => {
      vi.useFakeTimers()
      let state: SandboxSetupStatusPayload['state'] = 'setting_up'
      const { operations, scope, settings } = await createSandboxSettings({
        desktop: true,
        setupStatus: () => ({ state, platform: 'win32', message: '', requiresAdmin: false }),
        capabilityResult: {
          ...unavailableReport, available: true, code: 'ready', probeVersion: 0, capabilities: [],
        },
      })
      await settings.load()
      await settle()
      expect(settings.sandboxSetupStatus.value?.state).toBe('setting_up')
      state = finalState
      await vi.advanceTimersByTimeAsync(1_000)
      await settle()
      expect(settings.sandboxSetupStatus.value?.state).toBe(finalState)
      expect(settings.capability.value?.available === true).toBe(finalState === 'ready')
      const completedReads = operations.readiness.mock.calls.length
      await vi.advanceTimersByTimeAsync(60_000)
      expect(operations.readiness).toHaveBeenCalledTimes(completedReads)
      expect(operations.ensureReady).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('stops reading pending startup status when settings closes', async () => {
    vi.useFakeTimers()
    const { operations, scope, settings } = await createSandboxSettings({
      desktop: true, setupState: 'setting_up',
    })
    await settings.load()
    await settle()
    scope.stop()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(operations.readiness).toHaveBeenCalledOnce()
  })

  it('follows pending startup in a browser without initiating setup', async () => {
    vi.useFakeTimers()
    let initialized = false
    const { operations, scope, settings } = await createSandboxSettings({
      get capabilityResult() {
        return {
          ...unavailableReport,
          available: initialized,
          code: initialized ? 'ready' : 'setting_up',
          probeVersion: 0,
        }
      },
    })
    await settings.load()
    await settle()
    expect(settings.capability.value?.available).toBe(false)
    initialized = true
    await vi.advanceTimersByTimeAsync(1_000)
    expect(settings.capability.value?.available).toBe(true)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(operations.readiness).toHaveBeenCalledTimes(2)
    expect(operations.ensureReady).not.toHaveBeenCalled()
    scope.stop()
  })

  it('keeps following pending startup across a transient connection failure', async () => {
    vi.useFakeTimers()
    let state: SandboxSetupStatusPayload['state'] = 'setting_up'
    let disconnected = false
    const { operations, scope, settings } = await createSandboxSettings({
      desktop: true,
      setupStatus: () => {
        if (disconnected) throw new Error('connection interrupted')
        return { state, platform: 'win32', message: '', requiresAdmin: false }
      },
      get capabilityResult() {
        if (disconnected) throw new Error('connection interrupted')
        return { ...unavailableReport, available: true, code: 'ready', probeVersion: 0 }
      },
    })
    await settings.load()
    await settle()
    disconnected = true
    await vi.advanceTimersByTimeAsync(1_000)
    disconnected = false
    state = 'ready'
    await vi.advanceTimersByTimeAsync(1_000)
    expect(settings.sandboxSetupStatus.value?.state).toBe('ready')
    expect(settings.capability.value?.available).toBe(true)
    expect(operations.ensureReady).not.toHaveBeenCalled()
    scope.stop()
  })

  it.each([
    ['unavailable report', { capabilityResult: unavailableReport }],
    ['failed report', { capabilityError: true }],
  ])('does not automatically retry a %s after 10, 30, or 60 seconds', async (_label, options) => {
    vi.useFakeTimers()
    const { operations, scope, settings } = await createSandboxSettings(options)

    await settings.load()
    await settle()
    expect(operations.readiness).toHaveBeenCalledOnce()

    for (const elapsed of [10_000, 20_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(elapsed)
      await settle()
      expect(operations.readiness).toHaveBeenCalledOnce()
    }

    scope.stop()
  })

  it('performs exactly one forced check for an explicit retry', async () => {
    vi.useFakeTimers()
    const { operations, scope, settings } = await createSandboxSettings()

    await settings.load()
    await settle()
    await settings.loadCapability(true)

    expect(operations.readiness.mock.calls).toEqual([
      [{ refreshCapability: false }],
      [{ refreshCapability: true }],
    ])
    await vi.advanceTimersByTimeAsync(60_000)
    await settle()
    expect(operations.readiness).toHaveBeenCalledTimes(2)

    scope.stop()
  })

  it('performs exactly one forced refresh after successful setup', async () => {
    const { operations, scope, settings } = await createSandboxSettings({
      desktop: true,
      capabilityResult: { ...unavailableReport, available: true, code: 'ready' },
    })

    await settings.load()
    await settle()
    expect(operations.ensureReady).not.toHaveBeenCalled()

    await settings.ensureSandboxSetupForSafeMode()

    expect(operations.ensureReady).toHaveBeenCalledOnce()
    scope.stop()
  })

  it('ignores a stale capability result after its scope closes', async () => {
    vi.useFakeTimers()
    let resolveCapability!: (value: unknown) => void
    const pendingCapability = new Promise<unknown>((resolve) => {
      resolveCapability = resolve
    })
    const { operations, scope, settings } = await createSandboxSettings({
      capabilityResult: pendingCapability,
    })

    const loading = settings.loadCapability()
    await settle()
    scope.stop()
    resolveCapability({ ...unavailableReport, available: true, code: 'ready' })
    await loading
    await vi.advanceTimersByTimeAsync(60_000)

    expect(settings.capability.value).toBeNull()
    expect(operations.readiness).toHaveBeenCalledOnce()
  })
})

describe('useSandboxSettings runtime packs', () => {
  it('loads runtime status independently without blocking the policy page on failure', async () => {
    const { scope, settings } = await createSandboxSettings({
      runtimeStatusError: new Error('runtime service unavailable'),
    })

    await settings.load()
    await settle()

    expect(settings.ready.value).toBe(true)
    expect(settings.loadError.value).toBe('')
    expect(settings.runtimeStatus.value).toBeNull()
    expect(settings.runtimeStatusError.value).toBe('runtime service unavailable')
    scope.stop()
  })

  it('quietly falls back to legacy runtime versions for an old Gateway', async () => {
    const { scope, settings } = await createSandboxSettings()

    await settings.load()
    await settle()

    expect(settings.runtimeStatusSupported.value).toBe(false)
    expect(settings.runtimeStatusError.value).toBe('')
    expect(settings.runtimeVersions.value.python?.version).toBe('3.13.14')
    scope.stop()
  })

  it('uses exact action payloads and accepts direct operations or wrapped status', async () => {
    const queuedOperation: SandboxRuntimeOperation = {
      operationId: 'operation-1',
      componentId: 'python',
      kind: 'install',
      state: 'queued',
      downloadedBytes: 0,
      totalBytes: 100,
      progressPercent: 0,
      source: null,
      startedAtMs: 1,
      updatedAtMs: 1,
      error: null,
    }
    const { operations, scope, settings } = await createSandboxSettings({
      runtimeStatus: readyRuntimeStatus,
      runtimeAction: action => action === 'install'
        ? { kind: 'operation', operation: queuedOperation }
        : { kind: 'status', status: structuredClone(readyRuntimeStatus) },
    })
    await settings.load()
    await settle()

    await expect(settings.installRuntime('python')).resolves.toBe(true)
    expect(settings.runtimeStatus.value?.components[0]?.operation?.operationId)
      .toBe('operation-1')
    await expect(settings.cancelRuntime('python', 'operation-1')).resolves.toBe(true)
    await expect(settings.discardRuntimeDownload('python')).resolves.toBe(true)
    await expect(settings.removeRuntime('python')).resolves.toBe(true)

    expect(operations.installRuntime).toHaveBeenCalledWith('python')
    expect(operations.cancelRuntime).toHaveBeenCalledWith('python', 'operation-1')
    expect(operations.discardRuntimeDownload).toHaveBeenCalledWith('python')
    expect(operations.removeRuntime).toHaveBeenCalledWith('python')
    expect(settings.runtimeStatus.value?.catalogVersion).toBe('2026-08-21.2')
    scope.stop()
  })

  it('refreshes the row after a discard failure without blocking other runtime use', async () => {
    const paused = structuredClone(readyRuntimeStatus)
    paused.components[0] = {
      ...paused.components[0],
      resumeAvailable: true,
      resumeBytes: 40,
      operation: {
        operationId: 'cancelled-update',
        componentId: 'python',
        kind: 'install',
        state: 'cancelled',
        source: 'github',
        downloadedBytes: 40,
        totalBytes: 100,
        progressPercent: 40,
        startedAtMs: 1,
        updatedAtMs: 2,
        error: null,
      },
    }
    let statusCalls = 0
    const { operations, scope, settings } = await createSandboxSettings({
      runtimeStatus: () => {
        statusCalls += 1
        return statusCalls === 1 ? paused : readyRuntimeStatus
      },
      runtimeAction: action => {
        if (action === 'discard') throw new Error('cache is busy')
        return { kind: 'status', status: readyRuntimeStatus }
      },
    })
    await settings.load()
    await settle()

    await expect(settings.discardRuntimeDownload('python')).resolves.toBe(false)
    await settle()

    expect(operations.runtimeStatus).toHaveBeenCalledTimes(2)
    expect(statusCalls).toBe(2)
    expect(settings.runtimeStatus.value?.components[0]?.resumeBytes).toBe(0)
    expect(settings.runtimeActionError.python).toBe('cache is busy')
    expect(settings.runtimeActionPending.python).toBe(false)
    await expect(settings.removeRuntime('python')).resolves.toBe(true)
    expect(operations.removeRuntime).toHaveBeenCalledWith('python')
    scope.stop()
  })

  it('ignores a status response that predates a successful runtime action', async () => {
    let resolveStaleStatus!: (value: SandboxRuntimePackStatus) => void
    const staleStatus = new Promise<SandboxRuntimePackStatus>((resolve) => {
      resolveStaleStatus = resolve
    })
    let statusCalls = 0
    const queuedOperation: SandboxRuntimeOperation = {
      operationId: 'operation-1',
      componentId: 'python',
      kind: 'install',
      state: 'queued',
      downloadedBytes: 0,
      totalBytes: 100,
      progressPercent: 0,
      source: null,
      startedAtMs: 1,
      updatedAtMs: 1,
      error: null,
    }
    const { scope, settings } = await createSandboxSettings({
      runtimeStatus: () => {
        statusCalls += 1
        return statusCalls === 1 ? readyRuntimeStatus : staleStatus
      },
      runtimeAction: () => ({ kind: 'operation', operation: queuedOperation }),
    })
    await settings.load()
    await settle()

    settings.setRuntimeViewActive(true)
    await settle()
    expect(statusCalls).toBe(2)
    await expect(settings.installRuntime('python')).resolves.toBe(true)
    expect(settings.runtimeStatus.value?.components[0]?.operation?.operationId)
      .toBe('operation-1')

    resolveStaleStatus(structuredClone(readyRuntimeStatus))
    await settle()

    expect(settings.runtimeStatus.value?.components[0]?.operation?.operationId)
      .toBe('operation-1')
    expect(settings.runtimeStatusLoading.value).toBe(false)
    scope.stop()
  })

  it('polls after 750 ms only while the runtime view has an active operation', async () => {
    vi.useFakeTimers()
    let statusCalls = 0
    const downloading = structuredClone(readyRuntimeStatus)
    downloading.components[0] = {
      ...downloading.components[0],
      availability: 'missing',
      activeVersion: null,
      removable: false,
      operation: {
        operationId: 'operation-1',
        componentId: 'python',
        kind: 'install',
        state: 'downloading',
        source: 'oss',
        downloadedBytes: 50,
        totalBytes: 100,
        progressPercent: 50,
        startedAtMs: 1,
        updatedAtMs: 2,
        error: null,
      },
    }
    const { scope, settings } = await createSandboxSettings({
      runtimeStatus: () => {
        statusCalls += 1
        return statusCalls === 1 ? downloading : readyRuntimeStatus
      },
    })

    settings.setRuntimeViewActive(true)
    await settle()
    expect(statusCalls).toBe(1)

    await vi.advanceTimersByTimeAsync(749)
    expect(statusCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    await settle()
    expect(statusCalls).toBe(2)

    await vi.advanceTimersByTimeAsync(750)
    await settle()
    expect(statusCalls).toBe(2)
    settings.setRuntimeViewActive(false)
    scope.stop()
  })

  it('retries a transient status failure after five seconds while the runtime view is open', async () => {
    vi.useFakeTimers()
    let statusCalls = 0
    const { scope, settings } = await createSandboxSettings({
      runtimeStatus: () => {
        statusCalls += 1
        if (statusCalls === 1) throw new Error('temporary status failure')
        return readyRuntimeStatus
      },
    })

    settings.setRuntimeViewActive(true)
    await settle()
    expect(statusCalls).toBe(1)
    expect(settings.runtimeStatusError.value).toBe('temporary status failure')

    await vi.advanceTimersByTimeAsync(4_999)
    expect(statusCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    await settle()

    expect(statusCalls).toBe(2)
    expect(settings.runtimeStatus.value?.managementSupported).toBe(true)
    expect(settings.runtimeStatusError.value).toBe('')
    scope.stop()
  })
})
