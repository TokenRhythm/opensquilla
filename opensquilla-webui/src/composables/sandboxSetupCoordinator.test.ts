import { describe, expect, it, vi } from 'vitest'

import {
  ensureSandboxReady,
  normalizeSandboxSetupStatus,
  type SandboxSetupOperations,
} from './sandboxSetupCoordinator'

describe('sandboxSetupCoordinator', () => {
  function operations(
    call: (method: string, params?: Record<string, unknown>) => Promise<unknown>,
  ): SandboxSetupOperations {
    return {
      ensureSetup: () => call('sandbox.setup.ensure'),
      setupStatus: () => call('sandbox.setup.status'),
      capability: async () => await call(
        'sandbox.capability.status',
        { refresh: true },
      ) as { available: boolean } | null,
    }
  }

  it('normalizes snake_case administrator state', () => {
    expect(normalizeSandboxSetupStatus({
      state: 'not_setup',
      platform: 'win32',
      message: 'setup required',
      requires_admin: true,
    })).toEqual({
      state: 'not_setup',
      platform: 'win32',
      message: 'setup required',
      requiresAdmin: true,
      detail: undefined,
    })
  })

  it('does not report ready until live capability verification passes', async () => {
    const call = vi.fn()
      .mockResolvedValueOnce({ state: 'ready', platform: 'win32' })
      .mockResolvedValueOnce({ available: false })

    await expect(ensureSandboxReady(operations(call))).resolves.toMatchObject({
      ready: false,
      outcome: 'verification_failed',
      status: { state: 'ready' },
    })
    expect(call).toHaveBeenNthCalledWith(1, 'sandbox.setup.ensure')
    expect(call).toHaveBeenNthCalledWith(2, 'sandbox.capability.status', { refresh: true })
  })

  it('reports ready only after setup and live verification both pass', async () => {
    const call = vi.fn()
      .mockResolvedValueOnce({ state: 'ready', platform: 'win32' })
      .mockResolvedValueOnce({ available: true })

    await expect(ensureSandboxReady(operations(call))).resolves.toMatchObject({
      ready: true,
      outcome: 'ready',
      status: { state: 'ready' },
    })
  })

  it('classifies UAC cancellation and skips capability verification', async () => {
    const call = vi.fn().mockResolvedValue({
      state: 'failed',
      platform: 'win32',
      detail: 'cancelled_by_user',
    })

    await expect(ensureSandboxReady(operations(call))).resolves.toMatchObject({
      ready: false,
      outcome: 'cancelled',
      status: { state: 'failed' },
    })
    expect(call).toHaveBeenCalledOnce()
  })

  it('converts malformed payloads and transport failures into retryable failure', async () => {
    await expect(ensureSandboxReady(operations(vi.fn().mockResolvedValue({ state: 'future' }))))
      .resolves.toEqual({ ready: false, status: null, outcome: 'failed' })
    await expect(ensureSandboxReady(operations(vi.fn().mockRejectedValue(new Error('recycled')))))
      .resolves.toEqual({ ready: false, status: null, outcome: 'failed' })
  })

  it('recovers a completed setup after its response connection is recycled', async () => {
    const call = vi.fn()
      .mockRejectedValueOnce(new Error('Connection recycled after sandbox.setup.ensure terminated'))
      .mockResolvedValueOnce({ state: 'ready', platform: 'win32' })
      .mockResolvedValueOnce({ available: true })
    const ready = vi.fn().mockResolvedValue(undefined)

    await expect(ensureSandboxReady(operations(call), null, ready)).resolves.toMatchObject({
      ready: true,
      outcome: 'ready',
      status: { state: 'ready' },
    })
    expect(ready).toHaveBeenCalledOnce()
    expect(call).toHaveBeenNthCalledWith(1, 'sandbox.setup.ensure')
    expect(call).toHaveBeenNthCalledWith(2, 'sandbox.setup.status')
    expect(call).toHaveBeenNthCalledWith(3, 'sandbox.capability.status', { refresh: true })
  })
})
