// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'
import type {
  SandboxRuntime,
  SandboxRuntimeActionReceipt,
  SandboxSetupResult,
} from '@/modules/sandboxRuntime'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

const mounted: App[] = []

const policy = {
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
} as const

const runtimePackStatus: SandboxRuntimePackStatus = {
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
      installedBytes: 1234,
      removable: true,
      resumeAvailable: false,
      resumeBytes: 0,
      operation: null,
      lastError: null,
    },
    {
      componentId: 'node',
      availability: 'missing',
      catalogVersion: '2026-08-21.2',
      activeVersion: null,
      installedBytes: null,
      removable: false,
      resumeAvailable: false,
      resumeBytes: 0,
      operation: null,
      lastError: null,
    },
    {
      componentId: 'gitBash',
      availability: 'missing',
      catalogVersion: '2026-08-21.2',
      activeVersion: null,
      installedBytes: null,
      removable: false,
      resumeAvailable: false,
      resumeBytes: 0,
      operation: null,
      lastError: null,
    },
  ],
  nextPollAfterMs: 750,
}

async function settle() {
  for (let index = 0; index < 32; index++) await Promise.resolve()
}

function readySetupResult(): SandboxSetupResult {
  return {
    ready: true,
    outcome: 'ready',
    status: {
      state: 'ready',
      platform: 'win32',
      message: 'Sandbox setup is ready.',
      requiresAdmin: false,
    },
    capability: {
      available: true,
      backend: 'windows_default',
      platform: 'win32',
      code: 'ready',
      reason: 'ready',
      setupSupported: true,
      restartRequired: false,
      probeVersion: 1,
      capabilities: ['process'],
    },
  }
}

async function mountPanel(options: {
  capability?: Promise<unknown> | ((refreshCapability: boolean) => unknown)
  desktop?: boolean
  setupState?: 'not_setup' | 'setting_up' | 'ready' | 'failed' | 'unavailable'
  ensureState?: 'ready' | 'failed'
  ensureDetail?: string
  ensure?: Promise<SandboxSetupResult>
  runtimeTarget?: string
  runtimeStatus?: SandboxRuntimePackStatus | null
    | Promise<SandboxRuntimePackStatus | null> | (
    () => SandboxRuntimePackStatus | null | Promise<SandboxRuntimePackStatus | null>
  )
  runtimeStatusError?: Error
  runtimeAction?: (
    action: 'install' | 'cancel' | 'remove' | 'discard',
    componentId: string,
    operationId?: string,
  ) => SandboxRuntimeActionReceipt
  runtimePolicy?: {
    enabled: boolean
    python: boolean
    node: boolean
    gitBash: boolean
  }
  policyUpdateError?: Error
} = {}) {
  vi.resetModules()
  document.body.innerHTML = ''
  let currentRunMode: 'safe' | 'full' = 'full'

  const setupStatus = (
    state = options.setupState ?? 'ready',
  ): SandboxSetupStatusPayload => ({
    state,
    platform: 'win32',
    message: state === 'ready' ? 'Sandbox setup is ready.' : 'Sandbox setup is required.',
    requiresAdmin: state !== 'ready',
  })

  const capability = async (
    refreshCapability = false,
  ): Promise<SandboxCapabilityReport> => {
    if (typeof options.capability === 'function') {
      return await options.capability(refreshCapability) as SandboxCapabilityReport
    }
    if (options.capability) return await options.capability as SandboxCapabilityReport
    const setupReady = (options.setupState ?? 'ready') === 'ready'
      || (refreshCapability && (options.ensureState ?? 'ready') === 'ready')
    return {
      available: setupReady,
      backend: 'windows_default',
      platform: 'win32',
      code: setupReady ? 'ready' : 'setup_required',
      reason: setupReady ? 'ready' : 'setup required',
      setupSupported: true,
      restartRequired: false,
      probeVersion: 1,
      capabilities: setupReady ? ['process'] : [],
    }
  }

  const actionReceipt = (
    action: 'install' | 'cancel' | 'remove' | 'discard',
    componentId: string,
    operationId?: string,
  ): SandboxRuntimeActionReceipt => {
    return options.runtimeAction?.(action, componentId, operationId)
      ?? { kind: 'status', status: structuredClone(runtimePackStatus) }
  }

  const readiness = vi.fn(async (request?: { refreshCapability?: boolean }) => {
    const status = setupStatus()
    const report = status.state === 'ready'
      ? await capability(request?.refreshCapability === true)
      : null
    return { status, capability: report }
  })
  const ensureReady = vi.fn(async (): Promise<SandboxSetupResult> => {
    if (options.ensure) return await options.ensure
    const state = options.ensureState ?? 'ready'
    const status = {
      ...setupStatus(state),
      ...(options.ensureDetail ? { detail: options.ensureDetail } : {}),
    }
    if (state !== 'ready') {
      return {
        ready: false,
        status,
        capability: null,
        outcome: status.detail?.toLowerCase().includes('cancel')
          ? 'cancelled' as const
          : 'failed' as const,
      }
    }
    const report = await capability(true)
    return report.available
      ? { ready: true, status, capability: report, outcome: 'ready' as const }
      : {
          ready: false,
          status,
          capability: report,
          outcome: 'verification_failed' as const,
        }
  })
  const loadSettings = vi.fn(async () => {
    const loadedPolicy = JSON.parse(JSON.stringify(policy)) as SandboxPolicy
    if (options.runtimePolicy) loadedPolicy.runtimes = structuredClone(options.runtimePolicy)
    return {
      policy: loadedPolicy,
      defaults: {
        builtinDenyWritePaths: ['C:\\Users\\tester\\.ssh'],
        runtimeTarget: options.runtimeTarget ?? 'windows-x64',
        runtimeVersions: {
          python: { version: '3.13.14', available: true },
          node: { version: '24.18.1', available: true },
          gitBash: { version: '2.55.0', available: true },
        },
      },
      preference: { runMode: currentRunMode, source: 'preference' },
    }
  })
  const updatePolicy = vi.fn(async (basePolicyVersion: number, value: SandboxPolicy) => {
    if (options.policyUpdateError) throw options.policyUpdateError
    return { ...structuredClone(value), policyVersion: basePolicyVersion + 1 }
  })
  const preference = vi.fn(async () => ({
    runMode: currentRunMode,
    source: 'preference',
  }))
  const selectMode = vi.fn(async (mode: 'safe' | 'full') => {
    currentRunMode = mode
    return { runMode: currentRunMode, source: 'preference' }
  })
  const runtimeStatus = vi.fn(async () => {
    if (options.runtimeStatusError) throw options.runtimeStatusError
    if (typeof options.runtimeStatus === 'function') {
      return await options.runtimeStatus() as SandboxRuntimePackStatus | null
    }
    if (options.runtimeStatus !== undefined) {
      return await options.runtimeStatus
    }
    return structuredClone(runtimePackStatus)
  })
  const installRuntime = vi.fn(async (componentId: 'python' | 'node' | 'gitBash') => (
    actionReceipt('install', componentId)
  ))
  const cancelRuntime = vi.fn(async (
    componentId: 'python' | 'node' | 'gitBash',
    operationId: string,
  ) => actionReceipt('cancel', componentId, operationId))
  const removeRuntime = vi.fn(async (componentId: 'python' | 'node' | 'gitBash') => (
    actionReceipt('remove', componentId)
  ))
  const discardRuntimeDownload = vi.fn(async (
    componentId: 'python' | 'node' | 'gitBash',
  ) => actionReceipt('discard', componentId))

  const sandbox: SandboxRuntime = {
    readiness,
    ensureReady,
    loadSettings,
    updatePolicy,
    preference,
    selectMode,
    onPreferenceChanged: () => () => undefined,
    runtimeStatus,
    installRuntime,
    cancelRuntime,
    removeRuntime,
    discardRuntimeDownload,
    async resumeSession(sessionKey) {
      return { sessionKey, resumed: true, autonomousPaused: false }
    },
  }
  vi.doMock('@/platform', () => ({
    usePlatform: () => ({
      id: options.desktop === false ? 'web' : 'desktop',
      capabilities: { isDesktop: options.desktop !== false },
      settings: {},
    }),
  }))

  const { createApp } = await import('vue')
  const { createPinia } = await import('pinia')
  const i18n = (await import('@/i18n')).default
  const { SANDBOX_RUNTIME_KEY } = await import('@/modules/sandboxRuntime')
  i18n.global.locale.value = 'en'
  const Component = (await import('./SandboxSettingsPanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(createPinia())
  app.use(i18n)
  app.provide(SANDBOX_RUNTIME_KEY, sandbox)
  app.mount(el)
  mounted.push(app)
  await settle()
  const unmount = () => {
    const index = mounted.indexOf(app)
    if (index >= 0) mounted.splice(index, 1)
    app.unmount()
  }
  return {
    el,
    operations: {
      readiness,
      ensureReady,
      loadSettings,
      updatePolicy,
      preference,
      selectMode,
      runtimeStatus,
      installRuntime,
      cancelRuntime,
      removeRuntime,
      discardRuntimeDownload,
    },
    unmount,
  }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  vi.doUnmock('@/platform')
  vi.restoreAllMocks()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('SandboxSettingsPanel', () => {
  it('starts with a quiet overview and keeps rule editors out of sight', async () => {
    const { el } = await mountPanel()

    expect(el.querySelector('.sandbox-settings__eyebrow')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
    expect(el.querySelectorAll('[data-testid^="sandbox-open-"]')).toHaveLength(4)
    expect(el.querySelector('[data-testid="builtin-file-rules"]')).toBeNull()
    expect(el.querySelector('[data-testid="create-sandbox-token"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-open-advanced"]')).toBeNull()
    expect(el.querySelector('[data-testid="save-sandbox-section"]')).toBeNull()
  }, 15_000)

  it('opens focused details and returns without saving', async () => {
    const { el, operations } = await mountPanel()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="sandbox-detail"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="builtin-file-rules"]')?.textContent)
      .toContain('C:\\Users\\tester\\.ssh')

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-detail-back"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
    expect(operations.updatePolicy).not.toHaveBeenCalled()
  }, 15_000)

  it('loads immutable file rules and immediately saves an added custom rule', async () => {
    const { el, operations } = await mountPanel()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="builtin-file-rules"]')?.textContent)
      .toContain('C:\\Users\\tester\\.ssh')

    const input = el.querySelector<HTMLInputElement>('input[placeholder="Add a protected path"]')!
    input.value = 'D:\\Secrets'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        files: expect.objectContaining({
          customDenyWritePaths: ['D:\\Secrets'],
        }),
      }),
    )
  })

  it('clamps the recursive-delete backup quota to the visible 0.1 GiB minimum', async () => {
    vi.useFakeTimers()
    const { el, operations } = await mountPanel()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    const input = el.querySelector<HTMLInputElement>('[data-testid="sandbox-backup-quota"]')!
    input.value = '0'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.advanceTimersByTimeAsync(500)
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        files: expect.objectContaining({
          backupQuotaBytes: Math.ceil(0.1 * 1024 ** 3),
        }),
      }),
    )
  })

  it('does not expose or load named-token management', async () => {
    const { el } = await mountPanel()

    expect(el.textContent).not.toContain('Named Token')
    expect(el.querySelector('[data-testid="create-sandbox-token"]')).toBeNull()
  })

  it('renders policy controls without waiting for live capability verification', async () => {
    const capability = new Promise<unknown>(() => {})
    const { el } = await mountPanel({ capability })

    expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
    expect(el.querySelector<HTMLButtonElement>('[data-testid="sandbox-default-mode"] button')?.disabled)
      .toBe(true)
    expect(el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')?.disabled)
      .toBe(false)
  })

  it('immediately persists an available Safe mode selection without Save or Discard', async () => {
    const { el, operations } = await mountPanel()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()

    expect(operations.selectMode).toHaveBeenCalledWith('safe')
    expect(el.querySelector('[data-testid="save-sandbox-section"]')).toBeNull()
    await vi.waitFor(() => {
      expect(el.querySelector('[data-testid="sandbox-safe-mode"]')?.classList.contains('is-selected'))
        .toBe(true)
    })
  })

  it('does not retry an unavailable live capability in the background', async () => {
    vi.useFakeTimers()
    let attempts = 0
    const { operations } = await mountPanel({
      capability: () => {
        attempts += 1
        return {
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
      },
    })

    const initialReads = operations.readiness.mock.calls.length
    expect(attempts).toBe(initialReads)
    for (const elapsed of [10_000, 20_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(elapsed)
      await settle()
      expect(operations.readiness).toHaveBeenCalledTimes(initialReads)
    }
  })

  it('does not retry capability verification after the panel is unmounted', async () => {
    vi.useFakeTimers()
    let rejectCapability!: (reason?: unknown) => void
    const capability = new Promise<unknown>((_resolve, reject) => {
      rejectCapability = reject
    })
    const { operations, unmount } = await mountPanel({ capability })

    expect(operations.readiness).toHaveBeenCalledOnce()
    unmount()
    rejectCapability(new Error('connection closed'))
    await settle()
    await vi.advanceTimersByTimeAsync(20_000)
    await settle()

    expect(operations.readiness).toHaveBeenCalledOnce()
  })

  it('does not expose desktop listener or CIDR configuration', async () => {
    const { el } = await mountPanel()

    expect(el.querySelector('[data-testid="sandbox-listen-lan"]')).toBeNull()
    expect(el.querySelector('input[placeholder="192.168.1.0/24"]')).toBeNull()
  })

  it('does not request setup until the local desktop user confirms', async () => {
    const { el, operations } = await mountPanel({ setupState: 'not_setup' })

    expect(operations.ensureReady).not.toHaveBeenCalled()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
    expect(operations.ensureReady).not.toHaveBeenCalled()
  })

  it('does not offer the setup action to a remote web client', async () => {
    const { el, operations } = await mountPanel({ desktop: false, setupState: 'not_setup' })
    const safeButton = el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!

    expect(safeButton.disabled).toBe(true)
    safeButton.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
    expect(operations.ensureReady).not.toHaveBeenCalled()
  })

  it.each(['failed', 'unavailable', 'setting_up'] as const)(
    'disables Safe mode when sandbox status is %s without opening setup',
    async setupState => {
      const { el, operations } = await mountPanel({ setupState })
      const safeButton = el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!

      expect(safeButton.disabled).toBe(true)
      safeButton.click()
      await settle()

      expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
      expect(operations.ensureReady).not.toHaveBeenCalled()
      expect(el.querySelector<HTMLButtonElement>('[data-testid="sandbox-full-mode"]')?.disabled)
        .toBe(false)
    },
  )

  it('allows cancelling first-time setup and opening it again without installing', async () => {
    const { el, operations } = await mountPanel({ setupState: 'not_setup' })
    const safeButton = el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!

    safeButton.click()
    await settle()
    const dialog = document.body.querySelector('[data-testid="sandbox-setup-confirm"]')!
    dialog.querySelector<HTMLButtonElement>('.btn:not(.btn--primary)')!.click()
    await settle()
    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
    expect(safeButton.disabled).toBe(false)

    safeButton.click()
    await settle()
    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
    expect(operations.ensureReady).not.toHaveBeenCalled()
    expect(operations.selectMode).not.toHaveBeenCalled()
  })

  it('shows neutral elapsed setup guidance while administrator approval is pending', async () => {
    vi.useFakeTimers()
    let resolveEnsure!: (value: SandboxSetupResult) => void
    const ensure = new Promise<SandboxSetupResult>((resolve) => {
      resolveEnsure = resolve
    })
    const { el } = await mountPanel({ setupState: 'not_setup', ensure })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent)
      .toContain('Confirm the Windows prompt to continue.')
    expect(document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')?.disabled)
      .toBe(true)

    await vi.advanceTimersByTimeAsync(5_000)
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent)
      .toContain('OpenSquilla is completing Safe mode setup. Keep the app open.')
    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()

    await vi.advanceTimersByTimeAsync(10_000)
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent)
      .toContain('First-time setup can take a few minutes. Verification will run automatically.')

    resolveEnsure(readySetupResult())
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')).toBeNull()
  })

  it('keeps the original setup progress active after same-tick repeated Continue clicks', async () => {
    vi.useFakeTimers()
    let resolveEnsure!: (value: SandboxSetupResult) => void
    const ensure = new Promise<SandboxSetupResult>((resolve) => {
      resolveEnsure = resolve
    })
    const { el } = await mountPanel({ setupState: 'not_setup', ensure })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    const continueButton = document.body.querySelector<HTMLButtonElement>(
      '[data-testid="sandbox-setup-continue"]',
    )!
    continueButton.click()
    continueButton.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent)
      .toContain('Confirm the Windows prompt to continue.')
    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
    expect(vi.getTimerCount()).toBe(1)

    await vi.advanceTimersByTimeAsync(5_000)
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent)
      .toContain('OpenSquilla is completing Safe mode setup. Keep the app open.')
    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()

    resolveEnsure(readySetupResult())
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')).toBeNull()
  })

  it('closes only the dialog when setup is moved to the background', async () => {
    let resolveEnsure!: (value: SandboxSetupResult) => void
    const ensure = new Promise<SandboxSetupResult>((resolve) => {
      resolveEnsure = resolve
    })
    const { el, operations } = await mountPanel({ setupState: 'not_setup', ensure })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-background"]')).toBeTruthy()
    expect(document.body.textContent).not.toContain('Cancel')
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-background"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
    expect(operations.ensureReady).toHaveBeenCalledOnce()

    resolveEnsure(readySetupResult())
    await settle()

    expect(operations.ensureReady).toHaveBeenCalledOnce()
  })

  it('forces live verification after setup and persists Safe mode automatically', async () => {
    const { el, operations } = await mountPanel({ setupState: 'not_setup' })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(operations.ensureReady).toHaveBeenCalledOnce()
    await vi.waitFor(() => {
      expect(el.querySelector('[data-testid="sandbox-safe-mode"]')?.classList.contains('is-selected'))
        .toBe(true)
    })
    expect(operations.selectMode).toHaveBeenCalledWith('safe')
  })

  it('soft-lands a cancelled UAC request without exposing helper details', async () => {
    const { el, operations } = await mountPanel({
      setupState: 'not_setup',
      ensureState: 'failed',
      ensureDetail: 'windows_setup_helper_cancelled',
    })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-full-mode"]')?.classList.contains('is-selected'))
      .toBe(true)
    expect(el.querySelector('[data-testid="sandbox-setup-result"]')?.textContent)
      .not.toContain('windows_setup_helper_cancelled')
    expect(operations.selectMode).not.toHaveBeenCalled()
  })

  it('renders compact runtime pack states without ambiguous policy switches', async () => {
    const { el } = await mountPanel()

    expect(el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent)
      .toContain('Python')
    expect(el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent)
      .not.toContain('Node.js')
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelectorAll('.sandbox-runtime-row')).toHaveLength(3)
    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('Installed · 3.13.15+20260814')
    expect(el.querySelector('[data-testid="sandbox-runtime-node"]')?.textContent)
      .toContain('Not installed')
    expect(el.querySelector('[data-testid="sandbox-runtime-install-node"]')).toBeTruthy()
    expect(el.querySelector('[data-testid^="sandbox-runtime-toggle-"]')).toBeNull()
    expect(el.querySelector('.sandbox-detail-header .sandbox-switch')).toBeNull()
  })

  it('does not project policy flags as installed runtimes while status is loading', async () => {
    let resolveStatus!: (value: SandboxRuntimePackStatus) => void
    const runtimeStatus = new Promise<SandboxRuntimePackStatus>((resolve) => {
      resolveStatus = resolve
    })
    const { el } = await mountPanel({ runtimeStatus })

    const summary = el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent
    expect(summary).toContain('Loading')
    expect(summary).not.toContain('Python · Node.js')

    resolveStatus(structuredClone(runtimePackStatus))
    await settle()
  })

  it('enables only the requested runtime before starting its download', async () => {
    const { el, operations } = await mountPanel({
      runtimePolicy: {
        enabled: false,
        python: true,
        node: true,
        gitBash: true,
      },
    })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-install-node"]')!.click()
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledOnce()
    expect(operations.installRuntime).toHaveBeenCalledWith('node')
    expect(operations.updatePolicy.mock.invocationCallOrder[0])
      .toBeLessThan(operations.installRuntime.mock.invocationCallOrder[0]!)
    expect(operations.updatePolicy).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        runtimes: {
          enabled: true,
          python: false,
          node: true,
          gitBash: false,
        },
      }),
    )
  })

  it('does not download when automatic runtime enabling cannot be saved', async () => {
    const { el, operations } = await mountPanel({
      runtimePolicy: {
        enabled: false,
        python: false,
        node: false,
        gitBash: false,
      },
      policyUpdateError: new Error('write rejected'),
    })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-install-node"]')!.click()
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledOnce()
    expect(operations.installRuntime).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="sandbox-runtime-node"]')?.textContent)
      .toContain('Save failed')
  })

  it('offers one explicit Enable action for an installed legacy-disabled runtime', async () => {
    const { el, operations } = await mountPanel({
      runtimePolicy: {
        enabled: false,
        python: false,
        node: false,
        gitBash: false,
      },
    })
    expect(el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent)
      .toContain('Python (Not enabled)')
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    const pythonRow = el.querySelector('[data-testid="sandbox-runtime-python"]')
    expect(pythonRow?.textContent).toContain('Installed · 3.13.15+20260814 · Not enabled')
    expect(el.querySelector('[data-testid="sandbox-runtime-enable-python"]')).toBeTruthy()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-enable-python"]')!.click()
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        runtimes: {
          enabled: true,
          python: true,
          node: false,
          gitBash: false,
        },
      }),
    )
    expect(operations.installRuntime).not.toHaveBeenCalled()
  })

  it('keeps the successful download source visible after installation', async () => {
    const installed = structuredClone(runtimePackStatus)
    installed.components[0].operation = {
      operationId: 'operation-installed',
      componentId: 'python',
      kind: 'install',
      state: 'completed',
      source: 'oss',
      downloadedBytes: 1234,
      totalBytes: 1234,
      progressPercent: 100,
      startedAtMs: 1,
      updatedAtMs: 2,
      error: null,
    }
    const { el } = await mountPanel({ runtimeStatus: installed })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    const pythonText = el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent
    expect(pythonText).toContain('1.2 KiB')
    expect(pythonText).toContain('Beijing OSS')
  })

  it('uses exact component action payloads from runtime rows', async () => {
    const downloading = structuredClone(runtimePackStatus)
    downloading.components[0] = {
      ...downloading.components[0],
      availability: 'missing',
      activeVersion: null,
      installedBytes: null,
      removable: false,
      operation: {
        operationId: 'operation-1',
        componentId: 'python',
        kind: 'install',
        state: 'downloading',
        source: 'oss',
        downloadedBytes: 40,
        totalBytes: 100,
        progressPercent: 40,
        startedAtMs: 1,
        updatedAtMs: 2,
        error: null,
      },
    }
    const { el, operations } = await mountPanel({ runtimeStatus: downloading })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('Downloading · 40%')
    const progress = el.querySelector<HTMLElement>('[role="progressbar"]')
    expect(progress?.getAttribute('aria-valuenow')).toBe('40')
    expect(el.querySelector('[data-testid="sandbox-runtime-discard-python"]')).toBeNull()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-cancel-python"]')!.click()
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-install-node"]')!.click()
    await settle()

    expect(operations.cancelRuntime).toHaveBeenCalledWith('python', 'operation-1')
    expect(operations.installRuntime).toHaveBeenCalledWith('node')
  })

  it('offers resume and discard for partial or complete cancelled downloads', async () => {
    for (const complete of [false, true]) {
      const cancelled = structuredClone(runtimePackStatus)
      cancelled.components[0] = {
        ...cancelled.components[0],
        availability: 'missing',
        activeVersion: null,
        installedBytes: null,
        removable: false,
        resumeAvailable: !complete,
        resumeBytes: complete ? 100 : 40,
        operation: {
          operationId: `cancelled-${complete}`,
          componentId: 'python',
          kind: 'install',
          state: 'cancelled',
          source: 'github',
          downloadedBytes: complete ? 100 : 40,
          totalBytes: 100,
          progressPercent: complete ? 100 : 40,
          startedAtMs: 1,
          updatedAtMs: 2,
          error: null,
        },
      }
      const { el, operations } = await mountPanel({ runtimeStatus: cancelled })
      el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
      await settle()

      expect(el.querySelector('[data-testid="sandbox-runtime-install-python"]')?.textContent)
        .toContain('Resume')
      expect(el.querySelector('[data-testid="sandbox-runtime-discard-python"]')?.textContent)
        .toContain('Discard download')
      expect(el.querySelector('[data-testid="sandbox-runtime-remove-python"]')).toBeNull()

      el.querySelector<HTMLButtonElement>(
        '[data-testid="sandbox-runtime-discard-python"]',
      )!.click()
      await settle()
      expect(operations.discardRuntimeDownload).toHaveBeenCalledWith('python')
      expect(el.querySelector('[data-testid="sandbox-runtime-discard-python"]')).toBeNull()
    }
  })

  it('keeps an installed runtime while exposing paused update actions', async () => {
    const updating = structuredClone(runtimePackStatus)
    updating.components[0] = {
      ...updating.components[0],
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
    const { el } = await mountPanel({ runtimeStatus: updating })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    const python = el.querySelector('[data-testid="sandbox-runtime-python"]')
    expect(python?.textContent)
      .toContain('Installed · 3.13.15+20260814 · Update paused')
    expect(el.querySelector('[data-testid="sandbox-runtime-install-python"]')?.textContent)
      .toContain('Resume')
    expect(el.querySelector('[data-testid="sandbox-runtime-discard-python"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="sandbox-runtime-remove-python"]')).toBeTruthy()
  })

  it('hides Git Bash for non-Windows runtime targets', async () => {
    const status = structuredClone(runtimePackStatus)
    status.target = 'darwin-arm64'
    const { el } = await mountPanel({ runtimeTarget: 'darwin-arm64', runtimeStatus: status })

    expect(el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent)
      .not.toContain('Git Bash')
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="sandbox-runtime-gitBash"]')).toBeNull()
  })

  it('falls back to legacy versions when runtime management is unavailable', async () => {
    const { el } = await mountPanel({ runtimeStatus: null })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('3.13.14')
    expect(el.querySelector('[data-testid^="sandbox-runtime-install-"]')).toBeNull()
    expect(el.querySelector('[data-testid^="sandbox-runtime-remove-"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-runtime-status-retry"]')).toBeNull()
  })

  it('can re-enable a legacy runtime when runtime management is unavailable', async () => {
    const { el, operations } = await mountPanel({
      runtimeStatus: null,
      runtimePolicy: {
        enabled: false,
        python: false,
        node: false,
        gitBash: false,
      },
    })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('3.13.14 · Not enabled')
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-enable-python"]')!.click()
    await settle()

    expect(operations.updatePolicy).toHaveBeenCalledOnce()
    expect(operations.installRuntime).not.toHaveBeenCalled()
  })

  it('keeps an explicit Enable failure inside the affected legacy runtime row', async () => {
    const { el, operations } = await mountPanel({
      runtimeStatus: null,
      runtimePolicy: {
        enabled: false,
        python: false,
        node: false,
        gitBash: false,
      },
      policyUpdateError: new Error('write rejected'),
    })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-enable-python"]')!.click()
    await settle()

    expect(operations.installRuntime).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('Save failed')
  })

  it('does not present unsupported managed runtimes as installed in the overview', async () => {
    const unsupported = structuredClone(runtimePackStatus)
    unsupported.managementSupported = false
    unsupported.components = unsupported.components.map(component => ({
      ...component,
      availability: 'unsupported',
      activeVersion: null,
      installedBytes: null,
      removable: false,
    }))
    const { el } = await mountPanel({ runtimeStatus: unsupported })

    const summary = el.querySelector('[data-testid="sandbox-open-runtimes"]')?.textContent
    expect(summary).toContain('Not available for this system')
    expect(summary).not.toContain('Python · Node.js')
  })

  it('keeps remove operations distinct from download actions', async () => {
    const status = structuredClone(runtimePackStatus)
    status.components[0] = {
      ...status.components[0],
      resumeAvailable: true,
      resumeBytes: 40,
      operation: {
        operationId: 'remove-1',
        componentId: 'python',
        kind: 'remove',
        state: 'failed',
        source: null,
        downloadedBytes: 0,
        totalBytes: null,
        progressPercent: 0,
        startedAtMs: 1,
        updatedAtMs: 2,
        error: null,
      },
    }
    const { el, operations } = await mountPanel({ runtimeStatus: status })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    const pythonRow = el.querySelector('[data-testid="sandbox-runtime-python"]')
    expect(pythonRow?.textContent).toContain('Removal failed')
    expect(pythonRow?.textContent).toContain('Retry removal')
    expect(el.querySelector('[data-testid="sandbox-runtime-install-python"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-runtime-cancel-python"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-runtime-discard-python"]')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-remove-python"]')!.click()
    await settle()
    expect(operations.removeRuntime).toHaveBeenCalledWith('python')
  })

  it('offers download again after a remove operation completes', async () => {
    const status = structuredClone(runtimePackStatus)
    status.components[0] = {
      ...status.components[0],
      availability: 'missing',
      activeVersion: null,
      installedBytes: null,
      removable: false,
      operation: {
        operationId: 'remove-complete-1',
        componentId: 'python',
        kind: 'remove',
        state: 'completed',
        source: null,
        downloadedBytes: 0,
        totalBytes: null,
        progressPercent: 0,
        startedAtMs: 1,
        updatedAtMs: 2,
        error: null,
      },
    }
    const { el } = await mountPanel({ runtimeStatus: status })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-install-python"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="sandbox-runtime-remove-python"]')).toBeNull()
  })

  it('keeps transient runtime status errors inside the runtime subpage and allows retry', async () => {
    const { el, operations } = await mountPanel({
      runtimeStatusError: new Error('runtime service unavailable'),
    })

    expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="sandbox-runtime-status-retry"]')).toBeNull()
    expect(el.textContent).not.toContain('runtime service unavailable')

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="sandbox-runtime-status-retry"]')).toBeTruthy()
    expect(el.textContent).toContain('Status unavailable')
    expect(el.textContent).not.toContain('runtime service unavailable')

    const beforeRetry = operations.runtimeStatus.mock.calls.length
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-status-retry"]')!.click()
    await settle()
    expect(operations.runtimeStatus).toHaveBeenCalledTimes(beforeRetry + 1)
  })
})
