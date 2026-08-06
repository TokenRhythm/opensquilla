import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const call = vi.hoisted(() => vi.fn())
const waitForConnection = vi.hoisted(() => vi.fn(async () => {}))
const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call, waitForConnection }),
}))

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

function readyStatus() {
  return {
    state: 'ready',
    platform: 'win32',
    message: 'Windows default sandbox is ready.',
    requiresAdmin: false,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  call.mockReset()
  waitForConnection.mockClear()
  pushToast.mockClear()
})

describe('sandbox setup store', () => {
  it('deduplicates setup and persists Safe after live verification', async () => {
    const ensure = deferred<ReturnType<typeof readyStatus>>()
    call.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'sandbox.setup.ensure') return ensure.promise
      if (method === 'sandbox.capability.status') {
        expect(params).toEqual({ refresh: true })
        return { available: true }
      }
      if (method === 'sandbox.run_mode.preference.set') return { runMode: params?.runMode }
      throw new Error(`unexpected method: ${method}`)
    })
    const { useSandboxSetupStore } = await import('./sandboxSetup')
    const store = useSandboxSetupStore()

    const first = store.startSafeSetup()
    const second = store.startSafeSetup()

    expect(store.ensuring).toBe(true)
    expect(call.mock.calls.filter(([method]) => method === 'sandbox.setup.ensure')).toHaveLength(1)

    ensure.resolve(readyStatus())
    await expect(Promise.all([first, second])).resolves.toEqual([true, true])

    expect(call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith('Safe mode is ready.', { tone: 'ok' })
    expect(store.ensuring).toBe(false)
    expect(store.outcome).toBe('ready')
  })

  it('keeps Full when the user explicitly selects it while setup runs', async () => {
    const ensure = deferred<ReturnType<typeof readyStatus>>()
    call.mockImplementation(async (method: string) => {
      if (method === 'sandbox.setup.ensure') return ensure.promise
      if (method === 'sandbox.capability.status') return { available: true }
      throw new Error(`unexpected method: ${method}`)
    })
    const { useSandboxSetupStore } = await import('./sandboxSetup')
    const store = useSandboxSetupStore()

    const pending = store.startSafeSetup()
    store.noteRunModeSelection('full')
    ensure.resolve(readyStatus())

    await expect(pending).resolves.toBe(true)
    expect(call.mock.calls.some(([method]) => method === 'sandbox.run_mode.preference.set')).toBe(false)
    expect(pushToast).toHaveBeenCalledWith('Safe mode is ready.', { tone: 'ok' })
  })

  it('retains Full and reports one failure when verification fails', async () => {
    call.mockImplementation(async (method: string) => {
      if (method === 'sandbox.setup.ensure') return readyStatus()
      if (method === 'sandbox.capability.status') return { available: false }
      throw new Error(`unexpected method: ${method}`)
    })
    const { useSandboxSetupStore } = await import('./sandboxSetup')
    const store = useSandboxSetupStore()

    await expect(store.startSafeSetup()).resolves.toBe(false)

    expect(store.outcome).toBe('verification_failed')
    expect(call.mock.calls.some(([method]) => method === 'sandbox.run_mode.preference.set')).toBe(false)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      'Safe mode setup could not finish. Try again from Safe mode.',
      { tone: 'danger' },
    )
  })

  it('allows a fresh retry after a completed operation', async () => {
    call.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'sandbox.setup.ensure') return readyStatus()
      if (method === 'sandbox.capability.status') return { available: true }
      if (method === 'sandbox.run_mode.preference.set') return { runMode: params?.runMode }
      throw new Error(`unexpected method: ${method}`)
    })
    const { useSandboxSetupStore } = await import('./sandboxSetup')
    const store = useSandboxSetupStore()

    await store.startSafeSetup()
    await store.startSafeSetup()

    expect(call.mock.calls.filter(([method]) => method === 'sandbox.setup.ensure')).toHaveLength(2)
    expect(pushToast).toHaveBeenCalledTimes(2)
  })
})
