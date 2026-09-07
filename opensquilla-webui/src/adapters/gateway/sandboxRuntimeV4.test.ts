import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions } from '@/lib/rpc'

import { createV4SandboxRuntime } from './sandboxRuntimeV4'

const policy = {
  schemaVersion: 2 as const,
  policyVersion: 0,
  files: {
    customDenyWritePaths: [],
    recursiveDeleteBackupEnabled: true,
    backupQuotaBytes: 1024,
  },
  commands: {
    requireApprovalPrefixes: [],
    autoAllowPrefixes: [],
    systemTools: 'auto' as const,
  },
  network: { blockAllNetwork: false, allowDomains: [], denyDomains: [] },
  runtimes: { enabled: true, python: true, node: true, gitBash: true },
}

function createTransports(
  request: (
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<unknown>,
) {
  const requestMock = vi.fn(request)
  const ready = vi.fn(async () => undefined)
  const markUnsupported = vi.fn()
  const rpc = {
    request: async <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<T> => await requestMock(method, params, options) as T,
    ready,
    supports: vi.fn(() => true),
    markUnsupported,
  }
  const events = {
    supports: vi.fn(() => true),
    subscribe: vi.fn((_event: string, _handler: (payload: unknown) => void) => ({
      close: vi.fn(),
    })),
  }
  return { rpc, events, request: requestMock, ready, markUnsupported }
}

describe('createV4SandboxRuntime', () => {
  it('maps semantic Settings operations to generated sandbox methods', async () => {
    const transports = createTransports(async (method, params) => {
      if (method === 'sandbox.policy.get') return structuredClone(policy)
      if (method === 'sandbox.policy.defaults') {
        return { builtinDenyWritePaths: [], runtimeTarget: null, runtimeVersions: {} }
      }
      if (method === 'sandbox.run_mode.preference.get') {
        return { runMode: 'full', source: 'default' }
      }
      if (method === 'sandbox.run_mode.preference.set') {
        return { runMode: params?.runMode, source: 'preference' }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.loadSettings()).resolves.toEqual({
      policy,
      defaults: { builtinDenyWritePaths: [], runtimeTarget: null, runtimeVersions: {} },
      preference: { runMode: 'full', source: 'default' },
    })
    await expect(runtime.selectMode('safe')).resolves.toEqual({
      runMode: 'safe',
      source: 'preference',
    })
    expect(transports.request).toHaveBeenCalledWith(
      'sandbox.run_mode.preference.set',
      { runMode: 'safe' },
      expect.any(Object),
    )
  })

  it('normalizes legacy setup aliases and validates event projections', async () => {
    const transports = createTransports(async method => {
      if (method === 'sandbox.setup.status') {
        return {
          state: 'not_setup',
          platform: 'win32',
          message: 'Setup required.',
          requires_admin: true,
        }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const handler = vi.fn()
    const close = vi.fn()
    transports.events.subscribe.mockImplementation((_event, callback) => {
      callback({ runMode: 'unknown', source: 'legacy' })
      callback({ runMode: 'safe', source: 'preference' })
      return { close }
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.readiness()).resolves.toEqual({
      status: {
        state: 'not_setup',
        platform: 'win32',
        message: 'Setup required.',
        requiresAdmin: true,
      },
      capability: null,
    })
    const unsubscribe = runtime.onPreferenceChanged(handler)
    expect(handler).toHaveBeenCalledOnce()
    expect(handler).toHaveBeenCalledWith({ runMode: 'safe', source: 'preference' })
    unsubscribe()
    expect(close).toHaveBeenCalledOnce()
  })

  it('subscribes before hello advertises event capabilities', () => {
    const transports = createTransports(async method => {
      throw new Error(`unexpected method: ${method}`)
    })
    transports.events.supports.mockReturnValue(false)
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    const unsubscribe = runtime.onPreferenceChanged(vi.fn())

    expect(transports.events.subscribe).toHaveBeenCalledWith(
      'sandbox.run_mode.preference.changed',
      expect.any(Function),
    )
    unsubscribe()
  })

  it('rejects an incomplete capability frame instead of inventing unavailable defaults', async () => {
    const transports = createTransports(async method => {
      if (method === 'sandbox.setup.status') {
        return {
          state: 'ready',
          platform: 'linux',
          message: 'Ready.',
          requiresAdmin: false,
        }
      }
      if (method === 'sandbox.capability.status') return {}
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.readiness()).rejects.toMatchObject({ code: 'invalid' })
  })

  it('rejects setup status with missing required fields before alias projection', async () => {
    const transports = createTransports(async method => {
      if (method === 'sandbox.setup.status') return { state: 'ready' }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.readiness()).rejects.toMatchObject({ code: 'invalid' })
  })

  it('rejects malformed Runtime Pack components instead of dropping them', async () => {
    const transports = createTransports(async method => {
      if (method === 'sandbox.runtime.status') {
        return {
          schemaVersion: 1,
          managementSupported: true,
          target: 'linux-x64',
          catalogVersion: '1',
          sourceOrder: ['oss'],
          components: [{ componentId: 'python', availability: 'future-state' }],
          nextPollAfterMs: 750,
        }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.runtimeStatus()).rejects.toMatchObject({ code: 'invalid' })
  })

  it.each(['RPC_TRANSPORT_ERROR', 'RPC_TIMEOUT'])(
    'never repeats setup after %s and reconciles by reads',
    async (failureCode) => {
      let setupCalls = 0
      const transports = createTransports(async method => {
        if (method === 'sandbox.setup.ensure') {
          setupCalls += 1
          throw Object.assign(new Error('socket closed during elevation'), {
            code: failureCode,
          })
        }
        if (method === 'sandbox.setup.status') {
          return {
            state: 'ready',
            platform: 'win32',
            message: 'Ready.',
            requiresAdmin: false,
          }
        }
        if (method === 'sandbox.capability.status') {
          return {
            available: true,
            backend: 'windows_default',
            platform: 'win32',
            code: 'ready',
            reason: 'ready',
            setupSupported: true,
            restartRequired: false,
            probeVersion: 1,
            capabilities: [],
          }
        }
        throw new Error(`unexpected method: ${method}`)
      })
      const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

      await expect(runtime.ensureReady()).resolves.toMatchObject({
        ready: true,
        outcome: 'ready',
        status: { state: 'ready' },
        capability: { available: true },
      })
      expect(setupCalls).toBe(1)
      expect(transports.ready).toHaveBeenCalledTimes(3)
    },
  )

  it('retries once when the transport proves the setup frame was not sent', async () => {
    let setupCalls = 0
    const transports = createTransports(async method => {
      if (method === 'sandbox.setup.ensure') {
        setupCalls += 1
        if (setupCalls === 1) {
          throw Object.assign(new Error('socket generation retired before send'), {
            code: 'RPC_TRANSPORT_ERROR',
            accepted: false,
          })
        }
        return {
          state: 'ready',
          platform: 'win32',
          message: 'Ready.',
          requiresAdmin: false,
        }
      }
      if (method === 'sandbox.capability.status') {
        return {
          available: true,
          backend: 'windows_default',
          platform: 'win32',
          code: 'ready',
          reason: 'ready',
          setupSupported: true,
          restartRequired: false,
          probeVersion: 1,
          capabilities: [],
        }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.ensureReady()).resolves.toMatchObject({ ready: true, outcome: 'ready' })
    expect(setupCalls).toBe(2)
  })

  it.each(['malformed success', 'contract internal error'])(
    'reconciles %s after send without repeating setup',
    async responseKind => {
      let setupCalls = 0
      let statusReads = 0
      const transports = createTransports(async (method, _params, options) => {
        if (method === 'sandbox.setup.ensure') {
          setupCalls += 1
          options?.onSent?.(1)
          if (responseKind === 'contract internal error') {
            throw Object.assign(new Error('response contract failed'), {
              code: 'INTERNAL_ERROR',
            })
          }
          return { state: 'ready' }
        }
        if (method === 'sandbox.setup.status') {
          statusReads += 1
          return {
            state: 'ready',
            platform: 'win32',
            message: 'Ready.',
            requiresAdmin: false,
          }
        }
        if (method === 'sandbox.capability.status') {
          return {
            available: true,
            backend: 'windows_default',
            platform: 'win32',
            code: 'ready',
            reason: 'ready',
            setupSupported: true,
            restartRequired: false,
            probeVersion: 1,
            capabilities: [],
          }
        }
        throw new Error(`unexpected method: ${method}`)
      })
      const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

      await expect(runtime.ensureReady()).resolves.toMatchObject({ ready: true, outcome: 'ready' })
      await expect(runtime.ensureReady()).resolves.toMatchObject({ ready: true, outcome: 'ready' })
      expect(setupCalls).toBe(1)
      expect(statusReads).toBe(2)
    },
  )

  it('preserves abort while verifying capability after setup', async () => {
    const controller = new AbortController()
    const transports = createTransports(async method => {
      if (method === 'sandbox.setup.ensure') {
        return {
          state: 'ready',
          platform: 'win32',
          message: 'Ready.',
          requiresAdmin: false,
        }
      }
      if (method === 'sandbox.capability.status') {
        controller.abort()
        throw Object.assign(new Error('capability read aborted'), { code: 'RPC_ABORTED' })
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.ensureReady({ signal: controller.signal })).rejects.toMatchObject({
      code: 'aborted',
    })
  })

  it.each([
    ['SANDBOX_CAPABILITY_UNAVAILABLE', 'setup_required'],
    ['STORAGE_BUSY', 'busy'],
  ])('maps %s to the semantic %s error', async (rpcError, domainError) => {
    const transports = createTransports(async method => {
      if (method === 'sandbox.run_mode.preference.set') {
        throw Object.assign(new Error(rpcError), { code: rpcError, retryable: true })
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

    await expect(runtime.selectMode('safe')).rejects.toMatchObject({ code: domainError })
  })

  it('keeps one setup lease while an ambiguous response remains setting_up', async () => {
    vi.useFakeTimers()
    try {
      let setupCalls = 0
      let statusReads = 0
      const transports = createTransports(async method => {
        if (method === 'sandbox.setup.ensure') {
          setupCalls += 1
          throw Object.assign(new Error('request timed out during elevation'), {
            code: 'RPC_TIMEOUT',
          })
        }
        if (method === 'sandbox.setup.status') {
          statusReads += 1
          if (statusReads === 2) {
            throw Object.assign(new Error('reconnect interrupted'), {
              code: 'RPC_TRANSPORT_ERROR',
            })
          }
          return statusReads === 1
            ? {
                state: 'setting_up',
                platform: 'win32',
                message: 'Sandbox initialization is running.',
                requiresAdmin: true,
              }
            : {
                state: 'ready',
                platform: 'win32',
                message: 'Ready.',
                requiresAdmin: false,
              }
        }
        if (method === 'sandbox.capability.status') {
          return {
            available: true,
            backend: 'windows_default',
            platform: 'win32',
            code: 'ready',
            reason: 'ready',
            setupSupported: true,
            restartRequired: false,
            probeVersion: 1,
            capabilities: [],
          }
        }
        throw new Error(`unexpected method: ${method}`)
      })
      const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

      const first = runtime.ensureReady()
      const second = runtime.ensureReady()
      await vi.runAllTimersAsync()

      await expect(Promise.all([first, second])).resolves.toEqual([
        expect.objectContaining({ ready: true, outcome: 'ready' }),
        expect.objectContaining({ ready: true, outcome: 'ready' }),
      ])
      expect(setupCalls).toBe(1)
      expect(statusReads).toBe(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('bounds a stuck ambiguous lease and keeps later retries read-only', async () => {
    vi.useFakeTimers()
    try {
      let setupCalls = 0
      let terminal = false
      const transports = createTransports(async method => {
        if (method === 'sandbox.setup.ensure') {
          setupCalls += 1
          throw Object.assign(new Error('request timed out during elevation'), {
            code: 'RPC_TIMEOUT',
          })
        }
        if (method === 'sandbox.setup.status') {
          return terminal
            ? {
                state: 'ready',
                platform: 'win32',
                message: 'Ready.',
                requiresAdmin: false,
              }
            : {
                state: 'setting_up',
                platform: 'win32',
                message: 'Sandbox initialization is running.',
                requiresAdmin: true,
              }
        }
        if (method === 'sandbox.capability.status') {
          return {
            available: true,
            backend: 'windows_default',
            platform: 'win32',
            code: 'ready',
            reason: 'ready',
            setupSupported: true,
            restartRequired: false,
            probeVersion: 1,
            capabilities: [],
          }
        }
        throw new Error(`unexpected method: ${method}`)
      })
      const runtime = createV4SandboxRuntime(transports.rpc, transports.events)

      const first = runtime.ensureReady()
      await vi.runAllTimersAsync()
      await expect(first).resolves.toMatchObject({
        ready: false,
        outcome: 'in_progress',
        status: { state: 'setting_up' },
      })

      terminal = true
      const reconciled = runtime.ensureReady()
      await vi.runAllTimersAsync()
      await expect(reconciled).resolves.toMatchObject({ ready: true, outcome: 'ready' })
      expect(setupCalls).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
