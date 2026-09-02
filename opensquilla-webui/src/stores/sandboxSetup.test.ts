import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'
import { createApp, type App } from 'vue'

import {
  SANDBOX_RUNTIME_KEY,
  type SandboxRuntime,
  type SandboxSetupResult,
} from '@/modules/sandboxRuntime'
import { useSandboxSetupStore } from './sandboxSetup'

const ensureReady = vi.hoisted(() => vi.fn<() => Promise<SandboxSetupResult>>())
const selectMode = vi.hoisted(() => vi.fn())
const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

vi.mock('@/i18n', () => ({
  default: {
    global: {
      t: (key: string) => ({
        'settings.sandbox.setup.readyToast': 'Safe mode is ready.',
        'settings.sandbox.setup.failedToast': 'Safe mode setup could not finish. Try again from Safe mode.',
      }[key] ?? key),
    },
  },
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function readyResult(ready = true): SandboxSetupResult {
  return {
    ready,
    status: {
      state: 'ready',
      platform: 'win32',
      message: 'Windows default sandbox is ready.',
      requiresAdmin: false,
    },
    capability: { available: ready } as SandboxSetupResult['capability'],
    outcome: ready ? 'ready' : 'verification_failed',
  }
}

function fakeRuntime(): SandboxRuntime {
  return {
    readiness: vi.fn(async () => ({ status: null, capability: null })),
    ensureReady,
    loadSettings: vi.fn(),
    updatePolicy: vi.fn(),
    preference: vi.fn(),
    selectMode,
    onPreferenceChanged: vi.fn(() => () => undefined),
    runtimeStatus: vi.fn(),
    installRuntime: vi.fn(),
    cancelRuntime: vi.fn(),
    removeRuntime: vi.fn(),
    discardRuntimeDownload: vi.fn(),
    resumeSession: vi.fn(),
  }
}

const apps: App[] = []

function createStore() {
  const app = createApp({ template: '<div />' })
  const pinia = createPinia()
  app.use(pinia)
  app.provide(SANDBOX_RUNTIME_KEY, fakeRuntime())
  apps.push(app)
  return app.runWithContext(() => useSandboxSetupStore(pinia))
}

beforeEach(() => {
  ensureReady.mockReset()
  selectMode.mockReset()
  selectMode.mockResolvedValue({ runMode: 'safe', source: 'preference' })
  pushToast.mockClear()
})

afterEach(() => {
  while (apps.length) apps.pop()!.unmount()
})

describe('sandbox setup store', () => {
  it('deduplicates setup and persists Safe after live verification', async () => {
    const ensure = deferred<SandboxSetupResult>()
    ensureReady.mockReturnValue(ensure.promise)
    const store = createStore()

    const first = store.startSafeSetup()
    const second = store.startSafeSetup()

    expect(store.ensuring).toBe(true)
    expect(ensureReady).toHaveBeenCalledOnce()
    ensure.resolve(readyResult())
    await expect(Promise.all([first, second])).resolves.toEqual([true, true])

    expect(selectMode).toHaveBeenCalledWith('safe')
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith('Safe mode is ready.', { tone: 'ok' })
    expect(store.ensuring).toBe(false)
    expect(store.outcome).toBe('ready')
  })

  it('keeps Full when the user explicitly selects it while setup runs', async () => {
    const ensure = deferred<SandboxSetupResult>()
    ensureReady.mockReturnValue(ensure.promise)
    const store = createStore()

    const pending = store.startSafeSetup()
    store.noteRunModeSelection('full')
    ensure.resolve(readyResult())

    await expect(pending).resolves.toBe(true)
    expect(selectMode).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith('Safe mode is ready.', { tone: 'ok' })
  })

  it('reports one failure when capability verification fails', async () => {
    ensureReady.mockResolvedValue(readyResult(false))
    const store = createStore()

    await expect(store.startSafeSetup()).resolves.toBe(false)

    expect(store.outcome).toBe('verification_failed')
    expect(selectMode).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      'Safe mode setup could not finish. Try again from Safe mode.',
      { tone: 'danger' },
    )
  })

  it('keeps an ambiguous server-side setup in progress without a failure toast', async () => {
    ensureReady.mockResolvedValue({
      ready: false,
      status: {
        state: 'setting_up',
        platform: 'win32',
        message: 'Sandbox initialization is running.',
        requiresAdmin: true,
      },
      capability: null,
      outcome: 'in_progress',
    })
    const store = createStore()

    await expect(store.startSafeSetup()).resolves.toBe(false)

    expect(store.outcome).toBe('in_progress')
    expect(pushToast).not.toHaveBeenCalled()
    expect(selectMode).not.toHaveBeenCalled()
  })

  it('allows a fresh retry after a completed operation', async () => {
    ensureReady.mockResolvedValue(readyResult())
    const store = createStore()

    await expect(store.startSafeSetup()).resolves.toBe(true)
    await expect(store.startSafeSetup()).resolves.toBe(true)

    expect(ensureReady).toHaveBeenCalledTimes(2)
    expect(selectMode).toHaveBeenCalledTimes(2)
    expect(pushToast).toHaveBeenCalledTimes(2)
  })
})
