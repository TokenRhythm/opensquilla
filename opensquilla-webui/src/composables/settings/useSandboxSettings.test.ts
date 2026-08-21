import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EffectScope } from 'vue'
import type { SandboxPolicy, SandboxRuntimePackStatus } from '@/types/sandbox'

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
  catalogVersion: '2026-07-30.1',
  sourceOrder: ['oss', 'github'],
  components: [
    {
      componentId: 'python',
      availability: 'ready',
      catalogVersion: '2026-07-30.1',
      activeVersion: '3.13.14+20260728',
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
  setupState?: 'not_setup' | 'ready'
  policyUpdate?: (params: Record<string, unknown>) => unknown | Promise<unknown>
  runModeSetError?: Error
  runtimeStatus?: unknown | (() => unknown | Promise<unknown>)
  runtimeStatusError?: Error
  runtimeAction?: (method: string, params?: Record<string, unknown>) => unknown
} = {}) {
  vi.resetModules()
  const pushToast = vi.fn()
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'sandbox.policy.get') return structuredClone(policy)
    if (method === 'sandbox.policy.defaults') {
      return {
        runtimeTarget: 'windows-x64',
        runtimeVersions: { python: { version: '3.13.14', available: true } },
      }
    }
    if (method === 'sandbox.run_mode.preference.get') return { runMode: 'full' }
    if (method === 'sandbox.runtime.status') {
      if (options.runtimeStatusError) throw options.runtimeStatusError
      if (typeof options.runtimeStatus === 'function') return options.runtimeStatus()
      if (options.runtimeStatus !== undefined) return options.runtimeStatus
      throw Object.assign(new Error('method not found'), { code: 'METHOD_NOT_FOUND' })
    }
    if (
      method === 'sandbox.runtime.install'
      || method === 'sandbox.runtime.cancel'
      || method === 'sandbox.runtime.remove'
    ) {
      return options.runtimeAction?.(method, params) ?? { status: readyRuntimeStatus }
    }
    if (method === 'sandbox.capability.status') {
      if (options.capabilityError) throw new Error('probe failed')
      return options.capabilityResult ?? unavailableReport
    }
    if (method === 'sandbox.setup.status') {
      const state = options.setupState ?? 'not_setup'
      return {
        state,
        platform: 'win32',
        message: state === 'ready' ? 'ready' : 'setup required',
        requiresAdmin: state !== 'ready',
      }
    }
    if (method === 'sandbox.setup.ensure') {
      return {
        state: 'ready',
        platform: 'win32',
        message: 'ready',
        requiresAdmin: false,
      }
    }
    if (method === 'sandbox.run_mode.preference.set') {
      if (options.runModeSetError) throw options.runModeSetError
      return { runMode: params?.runMode, source: 'preference' }
    }
    if (method === 'sandbox.policy.update') {
      if (options.policyUpdate) return options.policyUpdate(params ?? {})
      const saved = structuredClone(params?.policy as typeof policy)
      saved.policyVersion = Number(params?.basePolicyVersion) + 1
      return saved
    }
    throw new Error(`unexpected method: ${method} ${JSON.stringify(params)}`)
  })
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      waitForConnection: vi.fn(async () => {}),
      call,
    }),
  }))
  vi.doMock('@/platform', () => ({
    usePlatform: () => ({
      capabilities: { isDesktop: options.desktop === true },
      settings: {},
    }),
  }))
  vi.doMock('@/composables/useToasts', () => ({
    useToasts: () => ({ pushToast }),
  }))

  const { effectScope } = await import('vue')
  const { useSandboxSettings } = await import('./useSandboxSettings')
  const scope: EffectScope = effectScope()
  const settings = scope.run(() => useSandboxSettings())!
  return { call, pushToast, scope, settings }
}

function capabilityCalls(call: ReturnType<typeof vi.fn>) {
  return call.mock.calls.filter(([method]) => method === 'sandbox.capability.status')
}

afterEach(() => {
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/platform')
  vi.doUnmock('@/composables/useToasts')
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useSandboxSettings auto-save', () => {
  it('persists a default mode selection without a separate save action', async () => {
    const { call, scope, settings } = await createSandboxSettings()
    await settings.load()

    await settings.setDefaultRunMode('safe')

    expect(call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
    expect(settings.defaultRunMode.value).toBe('safe')
    expect(settings.defaultRunModeBaseline.value).toBe('safe')
    scope.stop()
  })

  it('adopts a mode already persisted by the shared setup task without writing it twice', async () => {
    const { call, scope, settings } = await createSandboxSettings()
    await settings.load()

    settings.adoptSavedDefaultRunMode('safe')

    expect(settings.defaultRunMode.value).toBe('safe')
    expect(settings.defaultRunModeBaseline.value).toBe('safe')
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.run_mode.preference.set'))
      .toHaveLength(0)
    scope.stop()
  })

  it('debounces free-form section edits for 500 milliseconds', async () => {
    vi.useFakeTimers()
    const { call, scope, settings } = await createSandboxSettings()
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true

    settings.scheduleSectionSave('network')
    await vi.advanceTimersByTimeAsync(499)
    expect(call.mock.calls.some(([method]) => method === 'sandbox.policy.update')).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    await settle()
    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      basePolicyVersion: 0,
      policy: expect.objectContaining({
        network: expect.objectContaining({ blockAllNetwork: true }),
      }),
    }))
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
    const { call, scope, settings } = await createSandboxSettings({
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
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.policy.update')).toHaveLength(2)
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.policy.update')[1]?.[1])
      .toEqual(expect.objectContaining({
        basePolicyVersion: 1,
        policy: expect.objectContaining({
          network: expect.objectContaining({
            denyDomains: ['telemetry.example.com'],
          }),
        }),
      }))
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
    const conflict = Object.assign(new Error('policy version conflict'), {
      code: 'POLICY_VERSION_CONFLICT',
      details: { currentPolicy },
    })
    let updateCount = 0
    const { call, scope, settings } = await createSandboxSettings({
      policyUpdate: async (params) => {
        updateCount += 1
        if (updateCount === 1) throw conflict
        const saved = structuredClone(params.policy as typeof policy)
        saved.policyVersion = Number(params.basePolicyVersion) + 1
        return saved
      },
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true

    await expect(settings.flushSectionSave('network')).resolves.toBe(false)

    expect(settings.baseline.value).toEqual(currentPolicy)
    expect(settings.draft.value).toEqual(currentPolicy)

    settings.draft.value!.network.blockAllNetwork = true
    await expect(settings.flushSectionSave('network')).resolves.toBe(true)

    const updates = call.mock.calls.filter(([method]) => method === 'sandbox.policy.update')
    expect(updates).toHaveLength(2)
    expect(updates[1]?.[1]).toEqual(expect.objectContaining({
      basePolicyVersion: 1,
      policy: expect.objectContaining({
        network: expect.objectContaining({
          blockAllNetwork: true,
          denyDomains: ['desktop.example.com'],
        }),
      }),
    }))
    scope.stop()
  })

  it('keeps concurrent local drafts while adopting a conflict baseline', async () => {
    let rejectFirst!: (reason?: unknown) => void
    const first = new Promise((_resolve, reject) => { rejectFirst = reject })
    const currentPolicy = structuredClone(policy)
    currentPolicy.policyVersion = 1
    currentPolicy.network.denyDomains = ['desktop.example.com']
    const conflict = Object.assign(new Error('policy version conflict'), {
      code: 'POLICY_VERSION_CONFLICT',
      details: { currentPolicy },
    })
    const { scope, settings } = await createSandboxSettings({
      policyUpdate: async () => first,
    })
    await settings.load()
    settings.draft.value!.network.blockAllNetwork = true
    const saving = settings.flushSectionSave('network')
    await settle()

    settings.draft.value!.network.denyDomains.push('web.example.com')
    settings.draft.value!.files.customDenyWritePaths.push('/keep-local')
    rejectFirst(conflict)

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
  it.each([
    ['unavailable report', { capabilityResult: unavailableReport }],
    ['failed report', { capabilityError: true }],
  ])('does not automatically retry a %s after 10, 30, or 60 seconds', async (_label, options) => {
    vi.useFakeTimers()
    const { call, scope, settings } = await createSandboxSettings(options)

    await settings.load()
    await settle()
    expect(capabilityCalls(call)).toHaveLength(1)

    for (const elapsed of [10_000, 20_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(elapsed)
      await settle()
      expect(capabilityCalls(call)).toHaveLength(1)
    }

    scope.stop()
  })

  it('performs exactly one forced check for an explicit retry', async () => {
    vi.useFakeTimers()
    const { call, scope, settings } = await createSandboxSettings()

    await settings.load()
    await settle()
    await settings.loadCapability(true)

    expect(capabilityCalls(call)).toEqual([
      ['sandbox.capability.status', undefined],
      ['sandbox.capability.status', { refresh: true }],
    ])
    await vi.advanceTimersByTimeAsync(60_000)
    await settle()
    expect(capabilityCalls(call)).toHaveLength(2)

    scope.stop()
  })

  it('performs exactly one forced refresh after successful setup', async () => {
    const { call, scope, settings } = await createSandboxSettings({
      desktop: true,
      capabilityResult: { ...unavailableReport, available: true, code: 'ready' },
    })

    await settings.load()
    await settle()
    expect(capabilityCalls(call)).toHaveLength(0)

    await settings.ensureSandboxSetupForSafeMode()

    expect(capabilityCalls(call)).toEqual([
      ['sandbox.capability.status', { refresh: true }],
    ])
    scope.stop()
  })

  it('ignores a stale capability result after its scope closes', async () => {
    vi.useFakeTimers()
    let resolveCapability!: (value: unknown) => void
    const pendingCapability = new Promise<unknown>((resolve) => {
      resolveCapability = resolve
    })
    const { call, scope, settings } = await createSandboxSettings({
      capabilityResult: pendingCapability,
    })

    const loading = settings.loadCapability()
    await settle()
    scope.stop()
    resolveCapability({ ...unavailableReport, available: true, code: 'ready' })
    await loading
    await vi.advanceTimersByTimeAsync(60_000)

    expect(settings.capability.value).toBeNull()
    expect(capabilityCalls(call)).toHaveLength(1)
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
    const queuedOperation = {
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
    const { call, scope, settings } = await createSandboxSettings({
      runtimeStatus: readyRuntimeStatus,
      runtimeAction: method => method === 'sandbox.runtime.install'
        ? queuedOperation
        : { status: structuredClone(readyRuntimeStatus) },
    })
    await settings.load()
    await settle()

    await expect(settings.installRuntime('python')).resolves.toBe(true)
    expect(settings.runtimeStatus.value?.components[0]?.operation?.operationId)
      .toBe('operation-1')
    await expect(settings.cancelRuntime('python', 'operation-1')).resolves.toBe(true)
    await expect(settings.removeRuntime('python')).resolves.toBe(true)

    expect(call).toHaveBeenCalledWith('sandbox.runtime.install', { componentId: 'python' })
    expect(call).toHaveBeenCalledWith('sandbox.runtime.cancel', {
      componentId: 'python',
      operationId: 'operation-1',
    })
    expect(call).toHaveBeenCalledWith('sandbox.runtime.remove', { componentId: 'python' })
    expect(settings.runtimeStatus.value?.catalogVersion).toBe('2026-07-30.1')
    scope.stop()
  })

  it('ignores a status response that predates a successful runtime action', async () => {
    let resolveStaleStatus!: (value: SandboxRuntimePackStatus) => void
    const staleStatus = new Promise<SandboxRuntimePackStatus>((resolve) => {
      resolveStaleStatus = resolve
    })
    let statusCalls = 0
    const queuedOperation = {
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
      runtimeAction: () => queuedOperation,
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
