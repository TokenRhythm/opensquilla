// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

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
    if (method === 'sandbox.policy.get') return JSON.parse(JSON.stringify(policy))
    if (method === 'sandbox.policy.defaults') {
      return {
        builtinDenyWritePaths: ['C:\\Users\\tester\\.ssh'],
        runtimeTarget: 'windows-x64',
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
  })

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
  })

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
})
