// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'
import type { SandboxRuntimePackStatus } from '@/types/sandbox'

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
  for (let index = 0; index < 8; index++) await Promise.resolve()
}

async function mountPanel(options: {
  capability?: Promise<unknown> | ((params?: Record<string, unknown>) => unknown)
  desktop?: boolean
  setupState?: 'not_setup' | 'setting_up' | 'ready' | 'failed' | 'unavailable'
  ensureState?: 'ready' | 'failed'
  ensureDetail?: string
  ensure?: Promise<unknown>
  runtimeTarget?: string
  runtimeStatus?: unknown | ((params?: Record<string, unknown>) => unknown)
  runtimeStatusError?: Error
  runtimeAction?: (method: string, params?: Record<string, unknown>) => unknown
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
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'sandbox.capability.status') {
      if (typeof options.capability === 'function') return options.capability(params)
      if (options.capability) return options.capability
      const setupReady = (options.setupState ?? 'ready') === 'ready'
        || (params?.refresh === true && (options.ensureState ?? 'ready') === 'ready')
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
    if (method === 'sandbox.setup.status') {
      const state = options.setupState ?? 'ready'
      return {
        state,
        platform: 'win32',
        message: state === 'ready' ? 'Sandbox setup is ready.' : 'Sandbox setup is required.',
        requiresAdmin: state !== 'ready',
      }
    }
    if (method === 'sandbox.setup.ensure') {
      if (options.ensure) return options.ensure
      const state = options.ensureState ?? 'ready'
      return {
        state,
        platform: 'win32',
        message: state === 'ready' ? 'Sandbox setup is ready.' : 'Sandbox setup failed.',
        requiresAdmin: state !== 'ready',
        ...(options.ensureDetail ? { detail: options.ensureDetail } : {}),
      }
    }
    if (method === 'sandbox.policy.get') {
      const loadedPolicy = JSON.parse(JSON.stringify(policy))
      if (options.runtimePolicy) loadedPolicy.runtimes = structuredClone(options.runtimePolicy)
      return loadedPolicy
    }
    if (method === 'sandbox.runtime.status') {
      if (options.runtimeStatusError) throw options.runtimeStatusError
      if (typeof options.runtimeStatus === 'function') return options.runtimeStatus(params)
      if (options.runtimeStatus !== undefined) return options.runtimeStatus
      return structuredClone(runtimePackStatus)
    }
    if (
      method === 'sandbox.runtime.install'
      || method === 'sandbox.runtime.cancel'
      || method === 'sandbox.runtime.remove'
    ) {
      return options.runtimeAction?.(method, params) ?? {
        status: structuredClone(runtimePackStatus),
      }
    }
    if (method === 'sandbox.policy.defaults') {
      return {
        builtinDenyWritePaths: ['C:\\Users\\tester\\.ssh'],
        runtimeTarget: options.runtimeTarget ?? 'windows-x64',
        runtimeVersions: {
          python: { version: '3.13.14', available: true },
          node: { version: '24.18.1', available: true },
          gitBash: { version: '2.55.0', available: true },
        },
      }
    }
    if (method === 'sandbox.tokens.list') return { tokens: [] }
    if (method === 'sandbox.run_mode.preference.get') {
      return { runMode: currentRunMode, source: 'preference' }
    }
    if (method === 'config.get') {
      return {
        host: '127.0.0.1',
        auth: { allowed_client_cidrs: [] },
      }
    }
    if (method === 'sandbox.policy.update') {
      if (options.policyUpdateError) throw options.policyUpdateError
      const saved = JSON.parse(JSON.stringify(params?.policy))
      saved.policyVersion = Number(params?.basePolicyVersion) + 1
      return saved
    }
    if (method === 'sandbox.tokens.create') {
      return {
        token: 'osq_public_secret-once',
        record: {
          publicId: 'public',
          name: params?.name,
          capabilities: ['host.execute', 'task.read', 'task.submit'],
          createdAt: 1,
          lastUsedAt: null,
          lastPeer: null,
        },
      }
    }
    if (method === 'sandbox.tokens.revoke') return { revoked: true }
    if (method === 'sandbox.run_mode.preference.set') {
      currentRunMode = params?.runMode === 'safe' ? 'safe' : 'full'
      return { runMode: currentRunMode, source: 'preference' }
    }
    if (method === 'config.patch') return { restartRequired: true }
    throw new Error(`unexpected method: ${method}`)
  })
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      waitForConnection: vi.fn(async () => {}),
      call,
    }),
  }))
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
  i18n.global.locale.value = 'en'
  const Component = (await import('./SandboxSettingsPanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(createPinia())
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await settle()
  const unmount = () => {
    const index = mounted.indexOf(app)
    if (index >= 0) mounted.splice(index, 1)
    app.unmount()
  }
  return { el, call, unmount }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  vi.doUnmock('@/stores/rpc')
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
    const { el, call } = await mountPanel()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="sandbox-detail"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="builtin-file-rules"]')?.textContent)
      .toContain('C:\\Users\\tester\\.ssh')

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-detail-back"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
    expect(call.mock.calls.some(([method]) => method === 'sandbox.policy.update')).toBe(false)

    expect(call.mock.calls.some(([method]) => String(method).startsWith('sandbox.tokens.')))
      .toBe(false)
  }, 15_000)

  it('loads immutable file rules and immediately saves an added custom rule', async () => {
    const { el, call } = await mountPanel()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    expect(el.querySelector('[data-testid="builtin-file-rules"]')?.textContent)
      .toContain('C:\\Users\\tester\\.ssh')

    const input = el.querySelector<HTMLInputElement>('input[placeholder="Add a protected path"]')!
    input.value = 'D:\\Secrets'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      basePolicyVersion: 0,
      policy: expect.objectContaining({
        files: expect.objectContaining({
          customDenyWritePaths: ['D:\\Secrets'],
        }),
      }),
    }))
  })

  it('clamps the recursive-delete backup quota to the visible 0.1 GiB minimum', async () => {
    vi.useFakeTimers()
    const { el, call } = await mountPanel()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
    await settle()
    const input = el.querySelector<HTMLInputElement>('[data-testid="sandbox-backup-quota"]')!
    input.value = '0'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.advanceTimersByTimeAsync(500)
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      policy: expect.objectContaining({
        files: expect.objectContaining({
          backupQuotaBytes: Math.ceil(0.1 * 1024 ** 3),
        }),
      }),
    }))
  })

  it('does not expose or load named-token management', async () => {
    const { el, call } = await mountPanel()

    expect(el.textContent).not.toContain('Named Token')
    expect(el.querySelector('[data-testid="create-sandbox-token"]')).toBeNull()
    expect(call.mock.calls.some(([method]) => String(method).startsWith('sandbox.tokens.')))
      .toBe(false)
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
    const { el, call } = await mountPanel()

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
    expect(el.querySelector('[data-testid="save-sandbox-section"]')).toBeNull()
    await vi.waitFor(() => {
      expect(el.querySelector('[data-testid="sandbox-safe-mode"]')?.classList.contains('is-selected'))
        .toBe(true)
    })
  })

  it('does not retry an unavailable live capability in the background', async () => {
    vi.useFakeTimers()
    let attempts = 0
    const { call } = await mountPanel({
      capability: () => {
        attempts += 1
        return {
          available: attempts > 1,
          backend: 'windows_default',
          platform: 'win32',
          code: attempts > 1 ? 'ready' : 'probe_timeout',
          reason: attempts > 1 ? 'ready' : 'timed out',
          setupSupported: true,
          restartRequired: false,
          probeVersion: 1,
          capabilities: attempts > 1 ? ['process'] : [],
        }
      },
    })

    expect(attempts).toBe(1)
    for (const elapsed of [10_000, 20_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(elapsed)
      await settle()
      expect(attempts).toBe(1)
    }
    expect(call).toHaveBeenLastCalledWith('sandbox.capability.status', undefined)
  })

  it('does not retry capability verification after the panel is unmounted', async () => {
    vi.useFakeTimers()
    let rejectCapability!: (reason?: unknown) => void
    const capability = new Promise<unknown>((_resolve, reject) => {
      rejectCapability = reject
    })
    const { call, unmount } = await mountPanel({ capability })

    expect(call.mock.calls.filter(([method]) => method === 'sandbox.capability.status'))
      .toHaveLength(1)
    unmount()
    rejectCapability(new Error('connection closed'))
    await settle()
    await vi.advanceTimersByTimeAsync(20_000)
    await settle()

    expect(call.mock.calls.filter(([method]) => method === 'sandbox.capability.status'))
      .toHaveLength(1)
  })

  it('does not expose desktop listener or CIDR configuration', async () => {
    const { el, call } = await mountPanel()

    expect(el.querySelector('[data-testid="sandbox-listen-lan"]')).toBeNull()
    expect(el.querySelector('input[placeholder="192.168.1.0/24"]')).toBeNull()
    expect(call.mock.calls.some(([method]) => String(method).startsWith('config.'))).toBe(false)
  })

  it('does not request setup until the local desktop user confirms', async () => {
    const { el, call } = await mountPanel({ setupState: 'not_setup' })

    expect(call.mock.calls.some(([method]) => method === 'sandbox.capability.status')).toBe(false)

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
    expect(call.mock.calls.some(([method]) => method === 'sandbox.setup.ensure')).toBe(false)
  })

  it('does not offer the setup action to a remote web client', async () => {
    const { el, call } = await mountPanel({ desktop: false, setupState: 'not_setup' })
    const safeButton = el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!

    expect(safeButton.disabled).toBe(true)
    safeButton.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
    expect(call.mock.calls.some(([method]) => method === 'sandbox.setup.ensure')).toBe(false)
  })

  it('shows neutral elapsed setup guidance while administrator approval is pending', async () => {
    vi.useFakeTimers()
    let resolveEnsure!: (value: unknown) => void
    const ensure = new Promise<unknown>((resolve) => {
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

    resolveEnsure({
      state: 'ready',
      platform: 'win32',
      message: 'Sandbox setup is ready.',
      requiresAdmin: false,
    })
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')).toBeNull()
  })

  it('keeps the original setup progress active after same-tick repeated Continue clicks', async () => {
    vi.useFakeTimers()
    let resolveEnsure!: (value: unknown) => void
    const ensure = new Promise<unknown>((resolve) => {
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

    resolveEnsure({
      state: 'ready',
      platform: 'win32',
      message: 'Sandbox setup is ready.',
      requiresAdmin: false,
    })
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-progress"]')).toBeNull()
  })

  it('closes only the dialog when setup is moved to the background', async () => {
    let resolveEnsure!: (value: unknown) => void
    const ensure = new Promise<unknown>((resolve) => {
      resolveEnsure = resolve
    })
    const { el, call } = await mountPanel({ setupState: 'not_setup', ensure })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-background"]')).toBeTruthy()
    expect(document.body.textContent).not.toContain('Cancel')
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-background"]')!.click()
    await settle()

    expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeNull()
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.setup.ensure')).toHaveLength(1)

    resolveEnsure({
      state: 'ready',
      platform: 'win32',
      message: 'Sandbox setup is ready.',
      requiresAdmin: false,
    })
    await settle()

    expect(call.mock.calls.filter(([method]) => method === 'sandbox.setup.ensure')).toHaveLength(1)
  })

  it('forces live verification after setup and persists Safe mode automatically', async () => {
    const { el, call } = await mountPanel({ setupState: 'not_setup' })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-safe-mode"]')!.click()
    await settle()
    document.body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')!.click()
    await settle()

    expect(call.mock.calls.some(([method]) => method === 'sandbox.setup.ensure')).toBe(true)
    expect(call.mock.calls.some(([method, params]) => (
      method === 'sandbox.capability.status' && params?.refresh === true
    ))).toBe(true)
    await vi.waitFor(() => {
      expect(el.querySelector('[data-testid="sandbox-safe-mode"]')?.classList.contains('is-selected'))
        .toBe(true)
    })
    expect(call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
  })

  it('soft-lands a cancelled UAC request without exposing helper details', async () => {
    const { el, call } = await mountPanel({
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
    expect(call.mock.calls.some(([method]) => method === 'sandbox.run_mode.preference.set'))
      .toBe(false)
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
    const { el, call } = await mountPanel({
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

    const policyCallIndex = call.mock.calls.findIndex(
      ([method]) => method === 'sandbox.policy.update',
    )
    const installCallIndex = call.mock.calls.findIndex(
      ([method]) => method === 'sandbox.runtime.install',
    )
    expect(policyCallIndex).toBeGreaterThanOrEqual(0)
    expect(installCallIndex).toBeGreaterThan(policyCallIndex)
    expect(call.mock.calls[policyCallIndex]?.[1]).toEqual(expect.objectContaining({
      policy: expect.objectContaining({
        runtimes: {
          enabled: true,
          python: false,
          node: true,
          gitBash: false,
        },
      }),
    }))
  })

  it('does not download when automatic runtime enabling cannot be saved', async () => {
    const { el, call } = await mountPanel({
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

    expect(call.mock.calls.some(([method]) => method === 'sandbox.policy.update')).toBe(true)
    expect(call.mock.calls.some(([method]) => method === 'sandbox.runtime.install')).toBe(false)
    expect(el.querySelector('[data-testid="sandbox-runtime-node"]')?.textContent)
      .toContain('Save failed')
  })

  it('offers one explicit Enable action for an installed legacy-disabled runtime', async () => {
    const { el, call } = await mountPanel({
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

    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      policy: expect.objectContaining({
        runtimes: {
          enabled: true,
          python: true,
          node: false,
          gitBash: false,
        },
      }),
    }))
    expect(call.mock.calls.some(([method]) => method === 'sandbox.runtime.install')).toBe(false)
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
    const { el, call } = await mountPanel({ runtimeStatus: downloading })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('Downloading · 40%')
    const progress = el.querySelector<HTMLElement>('[role="progressbar"]')
    expect(progress?.getAttribute('aria-valuenow')).toBe('40')

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-cancel-python"]')!.click()
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-install-node"]')!.click()
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.runtime.cancel', {
      componentId: 'python',
      operationId: 'operation-1',
    })
    expect(call).toHaveBeenCalledWith('sandbox.runtime.install', { componentId: 'node' })
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

  it('falls back to legacy versions when the runtime RPC is unavailable', async () => {
    const methodNotFound = Object.assign(new Error('method not found'), {
      code: 'METHOD_NOT_FOUND',
    })
    const { el } = await mountPanel({ runtimeStatusError: methodNotFound })

    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    expect(el.querySelector('[data-testid="sandbox-runtime-python"]')?.textContent)
      .toContain('3.13.14')
    expect(el.querySelector('[data-testid^="sandbox-runtime-install-"]')).toBeNull()
    expect(el.querySelector('[data-testid^="sandbox-runtime-remove-"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-runtime-status-retry"]')).toBeNull()
  })

  it('can re-enable a legacy runtime when the management RPC is unavailable', async () => {
    const methodNotFound = Object.assign(new Error('method not found'), {
      code: 'METHOD_NOT_FOUND',
    })
    const { el, call } = await mountPanel({
      runtimeStatusError: methodNotFound,
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

    expect(call.mock.calls.some(([method]) => method === 'sandbox.policy.update')).toBe(true)
    expect(call.mock.calls.some(([method]) => method === 'sandbox.runtime.install')).toBe(false)
  })

  it('keeps an explicit Enable failure inside the affected legacy runtime row', async () => {
    const methodNotFound = Object.assign(new Error('method not found'), {
      code: 'METHOD_NOT_FOUND',
    })
    const { el, call } = await mountPanel({
      runtimeStatusError: methodNotFound,
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

    expect(call.mock.calls.some(([method]) => method === 'sandbox.runtime.install')).toBe(false)
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
    const { el, call } = await mountPanel({ runtimeStatus: status })
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-runtimes"]')!.click()
    await settle()

    const pythonRow = el.querySelector('[data-testid="sandbox-runtime-python"]')
    expect(pythonRow?.textContent).toContain('Removal failed')
    expect(pythonRow?.textContent).toContain('Retry removal')
    expect(el.querySelector('[data-testid="sandbox-runtime-install-python"]')).toBeNull()
    expect(el.querySelector('[data-testid="sandbox-runtime-cancel-python"]')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-remove-python"]')!.click()
    await settle()
    expect(call).toHaveBeenCalledWith('sandbox.runtime.remove', { componentId: 'python' })
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
    const { el, call } = await mountPanel({
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

    const beforeRetry = call.mock.calls.filter(
      ([method]) => method === 'sandbox.runtime.status',
    ).length
    el.querySelector<HTMLButtonElement>('[data-testid="sandbox-runtime-status-retry"]')!.click()
    await settle()
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.runtime.status'))
      .toHaveLength(beforeRetry + 1)
  })
})
