import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createV4GatewayAccess } from './gatewayAccessV4'

function memoryStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

function source() {
  return {
    state: 'disconnected' as 'disconnected' | 'connecting' | 'connected',
    error: null as string | null,
    isLocalOwner: false,
    canManageProjectWorkspaces: false,
    canChooseProject: false,
    auth: null as Record<string, unknown> | null,
    policy: null as Record<string, unknown> | null,
    connectionGeneration: 0,
    connect: vi.fn(async () => undefined),
    disconnect: vi.fn(),
    recoverConnectionGeneration: vi.fn(() => true),
  }
}

describe('createV4GatewayAccess', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', memoryStorage())
  })

  it('projects transport and hello data into semantic capabilities', () => {
    const raw = source()
    raw.state = 'connected'
    raw.isLocalOwner = true
    raw.canManageProjectWorkspaces = true
    raw.canChooseProject = true
    raw.auth = {
      principal: { authState: 'authenticated' },
      runModePolicy: {
        allowedRunModes: ['safe', 'full'],
        defaultRunMode: 'safe',
        ignoredWireField: true,
      },
    }
    raw.policy = {
      webui_stream_idle_grace_ms: 42_000,
      concurrent_history_reads: true,
      concurrent_optional_read_methods: ['sessions.messages.hydrate'],
    }
    raw.connectionGeneration = 7

    const access = createV4GatewayAccess(raw)

    expect(access.availability).toBe('available')
    expect(access.isAuthenticated).toBe(true)
    expect(access.canChooseProject).toBe(true)
    expect(access.runModePolicy).toEqual({
      allowedRunModes: ['safe', 'full'],
      defaultRunMode: 'safe',
      fullHostAccessDisabledReason: undefined,
    })
    expect(access.streamIdleTimeoutMs).toBe(42_000)
    expect(access.concurrentHistoryReads).toBe(true)
    expect(access.detachedSessionHydration).toBe(true)
    expect(access.subscriptionEpoch).toBe(7)
  })

  it('owns endpoint storage and delegates connection commands', async () => {
    const raw = source()
    localStorage.setItem('opensquilla.wsUrl', 'ws://saved.example/ws')
    const access = createV4GatewayAccess(raw)

    expect(access.loadConnectionEndpoint()).toBe('ws://saved.example/ws')
    await access.connect({ endpoint: ' ws://next.example/ws ', credential: ' secret ' })
    access.disconnect()

    expect(raw.connect).toHaveBeenCalledWith('ws://next.example/ws', 'secret')
    expect(raw.disconnect).toHaveBeenCalledOnce()
  })

  it('fails closed for malformed auth and stream policy projections', () => {
    const raw = source()
    raw.auth = { principal: 'owner', runModePolicy: [] }
    raw.policy = { webui_stream_idle_grace_ms: '42000' }
    const access = createV4GatewayAccess(raw)

    expect(access.isAuthenticated).toBe(false)
    expect(access.runModePolicy).toBeNull()
    expect(access.streamIdleTimeoutMs).toBeNull()
  })
})
