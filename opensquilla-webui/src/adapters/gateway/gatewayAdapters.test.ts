import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createGatewayAdapters } from './gatewayAdapters'

describe('Gateway Adapter composition', () => {
  it('exposes domain Modules without exposing the private transports', async () => {
    const call = vi.fn(async (method: string) => (
      method === 'sessions.pending_inputs.list'
        ? { items: [] }
        : { sessions: [] }
    )) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>
    const adapters = createGatewayAdapters({
      state: 'connected',
      error: null,
      isLocalOwner: true,
      canManageProjectWorkspaces: true,
      canChooseProject: true,
      auth: { principal: { authState: 'authenticated' } },
      policy: null,
      connectionGeneration: 1,
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
      'metaRunCenter',
      'appSettings',
      'providerConfiguration',
      'setupWorkflow',
      'migrationOperations',
      'workspaceCatalog',
      'sandboxRuntime',
      'sessionConversation',
      'usageReporting',
      'commandCatalog',
      'routeFeedback',
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
})
