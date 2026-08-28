import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createPrivateGatewayTransports } from './privateTransports'

function source() {
  return {
    connectionGeneration: 7,
    call: vi.fn(async () => ({ ok: true })) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>,
    on: vi.fn((_event: string, _handler: RpcEventHandler) => vi.fn()),
    supportsMethod: vi.fn((method: string) => method === 'sessions.list'),
    supportsEvent: vi.fn((event: string) => event === 'sessions.changed'),
    markMethodUnavailable: vi.fn(),
    waitForConnection: vi.fn(async () => undefined),
  }
}

describe('private Gateway transports', () => {
  it('delegates raw RPC requests and readiness without rewriting wire values', async () => {
    const rpcSource = source()
    const transports = createPrivateGatewayTransports(rpcSource)
    const controller = new AbortController()
    const callOptions: RpcCallOptions = {
      timeoutMs: 1234,
      signal: controller.signal,
      abortAction: 'reject',
    }

    await expect(transports.rpc.request(
      'sessions.list',
      { view: 'session-list-v1', limit: 25 },
      callOptions,
    )).resolves.toEqual({ ok: true })
    await transports.rpc.ready({
      timeoutMs: 4321,
      signal: controller.signal,
      timeoutAction: 'reject',
      abortAction: 'reconnect',
    })

    expect(rpcSource.call).toHaveBeenCalledWith(
      'sessions.list',
      { view: 'session-list-v1', limit: 25 },
      callOptions,
    )
    expect(rpcSource.waitForConnection).toHaveBeenCalledWith(
      4321,
      controller.signal,
      { timeoutAction: 'reject', abortAction: 'reconnect' },
    )
  })

  it('keeps capability and generation details inside the private seam', () => {
    const rpcSource = source()
    const transports = createPrivateGatewayTransports(rpcSource)

    expect(transports.rpc.supports('sessions.list')).toBe(true)
    expect(transports.events.supports('sessions.changed')).toBe(true)
    expect(transports.rpc.generation).toBe(7)
    transports.rpc.markUnsupported('legacy.method')

    expect(rpcSource.supportsMethod).toHaveBeenCalledWith('sessions.list')
    expect(rpcSource.supportsEvent).toHaveBeenCalledWith('sessions.changed')
    expect(rpcSource.markMethodUnavailable).toHaveBeenCalledWith('legacy.method')
  })

  it('owns idempotent event unsubscription', () => {
    const rpcSource = source()
    const unsubscribe = vi.fn()
    rpcSource.on.mockReturnValue(unsubscribe)
    const transports = createPrivateGatewayTransports(rpcSource)
    const handler = vi.fn()

    const subscription = transports.events.subscribe('sessions.changed', handler)
    subscription.close()
    subscription.close()

    expect(rpcSource.on).toHaveBeenCalledWith('sessions.changed', handler)
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

})
