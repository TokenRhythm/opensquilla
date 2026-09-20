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
    health: 'healthy' as 'healthy' | 'suspect',
    error: null as string | null,
    isLocalOwner: false,
    canManageProjectWorkspaces: false,
    canChooseProject: false,
    auth: null as Record<string, unknown> | null,
    policy: null as Record<string, unknown> | null,
    connectionGeneration: 0,
    deliveryContext: null as { targetId: string; principal: unknown } | null,
    connect: vi.fn(async () => undefined),
    disconnect: vi.fn(),
    recoverConnectionGeneration: vi.fn(() => true),
    hasRpcEvent: vi.fn((_event: string) => false),
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
    raw.hasRpcEvent.mockImplementation(event => event === 'session.event.turn_committed')

    const access = createV4GatewayAccess(raw)

    expect(access.availability).toBe('available')
    expect(access.connectionHealth).toBe('healthy')
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
    expect(access.turnCommittedEvents).toBe(true)
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

  it('projects suspect transport health separately from authenticated availability', () => {
    const raw = source()
    raw.state = 'connected'
    raw.health = 'suspect'
    const access = createV4GatewayAccess(raw)

    expect(access.availability).toBe('available')
    expect(access.isAvailable).toBe(true)
    expect(access.connectionHealth).toBe('suspect')
  })

  it('fails closed for malformed auth and stream policy projections', () => {
    const raw = source()
    raw.auth = { principal: 'owner', runModePolicy: [] }
    raw.policy = { webui_stream_idle_grace_ms: '42000' }
    const access = createV4GatewayAccess(raw)

    expect(access.isAuthenticated).toBe(false)
    expect(access.runModePolicy).toBeNull()
    expect(access.streamIdleTimeoutMs).toBeNull()
    expect(access.guestSessionOwnerId).toBeNull()
    expect(access.deliveryIdentity).toBeNull()
  })

  it('binds delivery to proven authority and canonicalizes only scope ordering', () => {
    const raw = source()
    const principal = {
      role: 'operator', authState: 'authenticated', authenticated: true, isOwner: false,
      scopes: ['operator.write', 'operator.read'], capabilities: ['chat.send', 'guest.safe'],
      tokenPublicId: 'synthetic-public-id', guestOwnerId: null,
      rawToken: 'must-not-be-serialized',
    }
    raw.deliveryContext = { targetId: 'opaque-target-a', principal }
    const access = createV4GatewayAccess(raw)
    const identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    expect(identity).not.toContain('must-not-be-serialized')
    raw.deliveryContext = { targetId: 'opaque-target-a', principal: {
      ...principal, scopes: [...principal.scopes].reverse(), capabilities: [...principal.capabilities].reverse(),
    } }
    expect(access.deliveryIdentity).toBe(identity)
    for (const changed of [
      { scopes: ['operator.read'] },
      { capabilities: ['chat.send'] },
      { tokenPublicId: 'another-public-id' },
      { isOwner: true },
    ]) {
      raw.deliveryContext = { targetId: 'opaque-target-a', principal: { ...principal, ...changed } }
      expect(access.deliveryIdentity).not.toBe(identity)
    }
    raw.deliveryContext = { targetId: 'opaque-target-b', principal }
    expect(access.deliveryIdentity).not.toBe(identity)
  })

  it('requires a well-formed identity proof and distinguishes anonymous owners', () => {
    const raw = source()
    const principal = {
      role: 'operator', authState: 'guest', authenticated: false, isOwner: false,
      scopes: ['operator.read'], capabilities: ['guest.safe'], guestOwnerId: 'a'.repeat(64),
    }
    raw.deliveryContext = { targetId: 'target', principal }
    const access = createV4GatewayAccess(raw)
    const identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    raw.deliveryContext = { targetId: 'target', principal: { ...principal, guestOwnerId: 'b'.repeat(64) } }
    expect(access.deliveryIdentity).not.toBe(identity)
    for (const changed of [
      { scopes: null }, { scopes: [42] }, { capabilities: 'guest.safe' },
      { role: null }, { guestOwnerId: null }, { authenticated: true }, { isOwner: true },
    ]) {
      raw.deliveryContext = { targetId: 'target', principal: { ...principal, ...changed } }
      expect(access.deliveryIdentity).toBeNull()
    }
    raw.deliveryContext = null
    expect(access.deliveryIdentity).toBeNull()
  })

  it('exposes a guest namespace only for the connected anonymous principal', () => {
    const raw = source()
    const ownerId = 'a'.repeat(64)
    raw.auth = { principal: {
      authState: 'guest', authenticated: false, isOwner: false, guestOwnerId: ownerId,
    } }
    const access = createV4GatewayAccess(raw)
    expect(access.guestSessionOwnerId).toBeNull()
    raw.state = 'connected'
    expect(access.guestSessionOwnerId).toBe(ownerId)
    raw.state = 'connecting'
    expect(access.guestSessionOwnerId).toBeNull()
  })

  it.each([
    { authState: 'authenticated', authenticated: false, isOwner: true },
    { authState: 'authenticated', authenticated: true, isOwner: false },
    { authState: 'guest', authenticated: true, isOwner: false },
    { authState: 'guest', authenticated: false, isOwner: true },
    { authState: 'guest', authenticated: false },
    { authState: 'guest', authenticated: false, isOwner: false, guestOwnerId: 'not-an-owner' },
    { authState: 'guest', authenticated: false, isOwner: false, guestOwnerId: 'A'.repeat(64) },
  ])('does not derive a guest namespace from ambiguous or owner authority: %j', principal => {
    const raw = source()
    raw.state = 'connected'
    raw.auth = { principal: { guestOwnerId: 'a'.repeat(64), ...principal } }
    expect(createV4GatewayAccess(raw).guestSessionOwnerId).toBeNull()
  })

  it.each(['authentication_failed', 'authentication_mismatch'])(
    'makes an explicit credential rejection actionable: %s', error => {
      const raw = source()
      raw.error = error
      expect(createV4GatewayAccess(raw).requiresCredential).toBe(true)
    },
  )

  it('does not request credentials for guests, scope denials, or transport policy failures', () => {
    const raw = source()
    const access = createV4GatewayAccess(raw)
    raw.state = 'connected'
    raw.auth = { principal: { authState: 'guest', authenticated: false } }
    expect(access.requiresCredential).toBe(false)
    for (const error of ['UNAUTHORIZED', '1008', 'protocol_rejected', 'Connection closed']) {
      raw.error = error
      expect(access.requiresCredential).toBe(false)
    }
  })
})

it.each([undefined, false, 'true', true])('advertises initial model only for boolean true (%s)', flag => {
  const raw = source()
  raw.policy = { chat_send_initial_model: flag }
  expect(createV4GatewayAccess(raw).chatSendInitialModel).toBe(flag === true)
})

it.each([undefined, false, 'true', true])('advertises session model selection only for boolean true (%s)', flag => {
  const raw = source()
  raw.policy = { sessions_routing_model_selection: flag }
  expect(createV4GatewayAccess(raw).sessionsRoutingModelSelection).toBe(flag === true)
})
