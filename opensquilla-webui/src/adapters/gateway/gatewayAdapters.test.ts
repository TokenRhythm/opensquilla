import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createGatewayAdapters } from './gatewayAdapters'

describe('Gateway Adapter composition', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('exposes domain Modules without exposing the private transports', async () => {
    const call = vi.fn(async (method: string) => (
      method === 'sessions.pending_inputs.list'
        ? { items: [] }
        : { sessions: [], count: 0, ts: 1 }
    )) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>
    const adapters = createGatewayAdapters({
      state: 'connected',
      health: 'healthy',
      error: null,
      isLocalOwner: true,
      getConnectionEndpoint: () => 'ws://gateway.example/ws',
      canManageProjectWorkspaces: true,
      canChooseProject: true,
      auth: { principal: { authState: 'authenticated' } },
      policy: null,
      connectionGeneration: 1,
      deliveryContext: null,
      connect: vi.fn(async () => undefined),
      disconnect: vi.fn(),
      recoverConnectionGeneration: vi.fn(() => true),
      call,
      on: vi.fn((_event: string, _handler: RpcEventHandler) => vi.fn()),
      hasRpcMethod: vi.fn(() => true),
      hasRpcEvent: vi.fn(() => true),
      rememberUnsupportedMethod: vi.fn(),
      ready: vi.fn(async () => undefined),
    })

    expect(Object.keys(adapters)).toEqual([
      'gatewayAccess',
      'conversationEvents',
      'sessionReadLifecycleFactory',
      'sessionInspection',
      'sessionDirectory',
      'sessionDirectoryChanges',
      'sessionLifecycle',
      'sessionRouting',
      'turnCommands',
      'pendingInputQueue',
      'approvalCenter',
      'goalCenter',
      'goalContinuity',
      'planCenter',
      'appSettings',
      'productActivity',
      'providerConfiguration',
      'setupWorkflow',
      'migrationOperations',
      'workspaceCatalog',
      'workspaceReferences',
      'workspaceFiles',
      'sandboxRuntime',
      'usageReporting',
      'commandCatalog',
      'promptCacheLease',
      'clarificationSubmission',
      'sessionMaintenance',
      'observability',
      'skillCatalog',
      'agentCatalog',
      'cronScheduler',
      'channelAdministration',
      'channelSetup',
      'artifactWorkbench',
      'memoryProfileImport',
      'audioTranscription',
    ])
    expect(adapters).not.toHaveProperty('rpc')
    expect(adapters).not.toHaveProperty('events')
    expect(adapters).not.toHaveProperty('sessionReadPort')
    await expect(adapters.sessionDirectory.listPage({ limit: 10 })).resolves.toEqual({
      items: [],
      hasMore: false,
      nextCursor: null,
    })
    expect(call).toHaveBeenCalledOnce()
    const changesSubscription = adapters.sessionDirectoryChanges.subscribe(vi.fn())
    await adapters.sessionDirectoryChanges.resume()
    expect(call).toHaveBeenCalledTimes(2)
    changesSubscription.close()

    await adapters.turnCommands.cancel({ sessionKey: 'agent:main:test', source: 'test' })
    expect(call).toHaveBeenLastCalledWith(
      'chat.abort',
      { sessionKey: 'agent:main:test', source: 'test' },
      undefined,
    )

    await expect(adapters.pendingInputQueue.list('agent:main:test')).resolves.toEqual([])
  })

  it('shares the current-target guard with support-bundle downloads after an endpoint switch', async () => {
    vi.stubGlobal('location', new URL('http://gateway.example/control/settings'))
    const endpoint = vi.fn(() => 'ws://gateway.example/ws')
    const requestBinary = vi.fn(async () => ({
      metadata: { status: 200, filename: 'support.zip' },
      blob: async () => new Blob(['bundle']),
      stream: () => null,
    }))
    const call = vi.fn(async () => ({ lines: ['remote gateway log'], cursor: 24, has_more: false }))
    const adapters = createGatewayAdapters({
      state: 'connected', health: 'healthy', error: null, isLocalOwner: true,
      getConnectionEndpoint: endpoint,
      canManageProjectWorkspaces: true, canChooseProject: true,
      auth: { principal: { authState: 'authenticated' } }, policy: null,
      connectionGeneration: 1, deliveryContext: null,
      connect: vi.fn(), disconnect: vi.fn(), recoverConnectionGeneration: vi.fn(() => true),
      call: call as Parameters<typeof createGatewayAdapters>[0]['call'],
      on: vi.fn(() => vi.fn()), hasRpcMethod: vi.fn(() => true),
      hasRpcEvent: vi.fn(() => true), rememberUnsupportedMethod: vi.fn(), ready: vi.fn(),
    }, {
      http: {
        requestBinary, requestJson: vi.fn(), requestBlob: vi.fn(),
        clearPreviewOrigin: vi.fn(), fetchExternalArtifact: vi.fn(),
      },
    })
    expect(adapters.gatewayAccess.supportBundleUnavailableReason).toBeNull()
    await expect(adapters.observability.downloadSupportBundle({ includeContent: false }))
      .resolves.toMatchObject({ filename: 'support.zip' })
    endpoint.mockReturnValue('ws://gateway.example:18791/ws')
    expect(adapters.gatewayAccess.supportBundleUnavailableReason).toBe('differentGateway')
    await expect(adapters.observability.downloadSupportBundle({ includeContent: false }))
      .rejects.toThrow('differentGateway')
    await expect(adapters.observability.tailLogs())
      .resolves.toEqual({ entries: ['remote gateway log'], truncated: false })
    expect(call).toHaveBeenCalledWith('logs.tail', { cursor: 0, limit: 200, level: null }, expect.any(Object))
    expect(requestBinary).toHaveBeenCalledOnce()
  })
})
