import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createPrivateGatewayTransports } from './privateTransports'
import { createV4SetupWorkflow } from './setupWorkflowV4'

function source() {
  return {
    connectionGeneration: 7,
    policy: { provider_probe_modes: ['model', 'reachability'] },
    call: vi.fn(async () => ({ ok: true })) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>,
    on: vi.fn((_event: string, _handler: RpcEventHandler) => vi.fn()),
    hasRpcMethod: vi.fn((method: string) => method === 'sessions.list'),
    hasRpcEvent: vi.fn((event: string) => event === 'sessions.changed'),
    rememberUnsupportedMethod: vi.fn(),
    ready: vi.fn(async () => undefined),
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
    expect(rpcSource.ready).toHaveBeenCalledWith(
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

    expect(rpcSource.hasRpcMethod).toHaveBeenCalledWith('sessions.list')
    expect(rpcSource.hasRpcEvent).toHaveBeenCalledWith('sessions.changed')
    expect(rpcSource.rememberUnsupportedMethod).toHaveBeenCalledWith('legacy.method')
  })

  it('projects the negotiated provider probe modes into the setup workflow capability', () => {
    const transports = createPrivateGatewayTransports(source())
    const workflow = createV4SetupWorkflow(transports.rpc)

    expect(workflow.capabilities.providerProbeModes).toBe(true)
  })

  it.each([
    undefined,
    null,
    {},
    { provider_probe_modes: 'model,reachability' },
    { provider_probe_modes: ['model'] },
    { provider_probe_modes: ['reachability'] },
    { provider_probe_modes: ['model', 'reachability', 1] },
  ])('keeps legacy probing for missing or incomplete policy %j', policy => {
    const transports = createPrivateGatewayTransports({ ...source(), policy })
    expect(createV4SetupWorkflow(transports.rpc).capabilities.providerProbeModes).toBe(false)
  })

  it('reflects a replacement connection policy without rebuilding the workflow', () => {
    const rpcSource = source()
    const workflow = createV4SetupWorkflow(createPrivateGatewayTransports(rpcSource).rpc)

    expect(workflow.capabilities.providerProbeModes).toBe(true)
    rpcSource.policy = { provider_probe_modes: ['model'] }
    expect(workflow.capabilities.providerProbeModes).toBe(false)
    rpcSource.policy = { provider_probe_modes: ['reachability', 'model'] }
    expect(workflow.capabilities.providerProbeModes).toBe(true)
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
