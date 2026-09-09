// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  RpcAbortError,
  RpcClient,
  type RpcClientError,
  RpcTimeoutError,
} from '@/lib/rpc'

class MockWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: MockWebSocket[] = []
  static initialReadyState = MockWebSocket.OPEN

  readonly sent: string[] = []
  throwOnSend = false
  readyState = MockWebSocket.initialReadyState
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this)
  }

  send(data: string): void {
    if (this.throwOnSend) throw new Error('send failed')
    this.sent.push(data)
  }

  close(code: number = 1000, reason: string = ''): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ code, reason, wasClean: code === 1000 } as CloseEvent)
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent)
  }
}

function pendingCount(client: RpcClient): number {
  return (
    client as unknown as {
      _pending: Map<string, unknown>
    }
  )._pending.size
}

function establishConnection(
  socket: MockWebSocket,
  policy: Record<string, unknown> = {},
): void {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    protocol: 3,
    policy: { tick_interval_ms: 30_000, ...policy },
  })
}

/** Most RPC tests exercise an already authenticated connection. */
function callOnReadySocket(client: RpcClient, ...args: Parameters<RpcClient['call']>) {
  if (client.state !== 'connected') establishConnection(MockWebSocket.instances[MockWebSocket.instances.length - 1])
  return client.call(...args)
}

describe('RpcClient', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    MockWebSocket.initialReadyState = MockWebSocket.OPEN
    localStorage.clear()
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('persists one random guest session key and sends it in every handshake', () => {
    const first = new RpcClient()
    first.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    firstSocket.receive({ type: 'event', event: 'connect.challenge' })

    const firstFrame = JSON.parse(firstSocket.sent[0]) as {
      params: { auth: { guestSessionKey: string }; caps: string[] }
    }
    const guestSessionKey = firstFrame.params.auth.guestSessionKey
    expect(firstFrame.params.caps).toEqual([
      'session.answer_generation_reset.v1',
      'session.turn_committed.v1',
      'transport.probe.v1',
    ])
    expect(guestSessionKey).toMatch(/^osqg_[A-Za-z0-9_-]{43}$/)
    expect(localStorage.getItem('opensquilla.guestSessionKey')).toBe(guestSessionKey)

    const second = new RpcClient()
    second.connect('ws://rpc.test')
    const secondSocket = MockWebSocket.instances[1]
    secondSocket.receive({ type: 'event', event: 'connect.challenge' })
    const secondFrame = JSON.parse(secondSocket.sent[0]) as {
      params: { auth: { guestSessionKey: string } }
    }
    expect(secondFrame.params.auth.guestSessionKey).toBe(guestSessionKey)

    first.disconnect()
    second.disconnect()
  })

  it('persists a server-generated compatibility guest key from hello', () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    const serverKey = 'osqg_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'

    socket.receive({
      protocol: 3,
      policy: { tick_interval_ms: 30_000 },
      auth: { guestSessionKey: serverKey },
    })

    expect(localStorage.getItem('opensquilla.guestSessionKey')).toBe(serverKey)
    client.disconnect()
  })

  it('sends the guest session key alongside a named token', () => {
    localStorage.setItem(
      'opensquilla.guestSessionKey',
      'osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    )
    const client = new RpcClient()
    client.connect('ws://rpc.test', 'osq_named_token')
    const socket = MockWebSocket.instances[0]
    socket.receive({ type: 'event', event: 'connect.challenge' })

    const frame = JSON.parse(socket.sent[socket.sent.length - 1]) as {
      params: { auth: { token: string; guestSessionKey: string } }
    }
    expect(frame.params.auth).toEqual({
      token: 'osq_named_token',
      guestSessionKey: 'osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    })
    client.disconnect()
  })

  it('preserves structured retry and acceptance metadata on the rejected error', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = callOnReadySocket(client,
      'chat.send',
      { message: 'hello' },
      { timeoutMs: 100, timeoutAction: 'reconnect' }
    )
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as { id: string }
    socket.receive({
      type: 'res',
      id: request.id,
      ok: false,
      error: {
        code: 'STORAGE_BUSY',
        message: 'Storage is temporarily busy',
        retryable: true,
        retry_after_ms: 250,
        accepted: false,
        details: { operation: 'upsert_session', waited_ms: 2000 },
      },
    })

    let caught: RpcClientError | undefined
    try {
      await result
    } catch (error) {
      caught = error as RpcClientError
    }

    expect(caught).toBeInstanceOf(Error)
    expect(caught).toMatchObject({
      message: 'Storage is temporarily busy',
      code: 'STORAGE_BUSY',
      retryable: true,
      retry_after_ms: 250,
      accepted: false,
      details: { operation: 'upsert_session', waited_ms: 2000 },
    })
    expect(pendingCount(client)).toBe(0)
    await vi.advanceTimersByTimeAsync(100)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('preserves the wire frame and leaves calls unbounded by default', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket, { tick_interval_ms: 1_000_000 })

    const result = callOnReadySocket(client, 'chat.history', { sessionKey: 'session-1' })
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as {
      type: string
      id: string
      method: string
      params: Record<string, unknown>
    }

    expect(request).toEqual({
      type: 'req',
      id: request.id,
      method: 'chat.history',
      params: { sessionKey: 'session-1' },
    })
    for (let i = 0; i < 4; i++) {
      await vi.advanceTimersByTimeAsync(30_000)
      socket.receive({ type: 'pong' })
    }
    expect(pendingCount(client)).toBe(1)

    socket.receive({ type: 'res', id: request.id, ok: true, payload: { messages: [] } })
    await expect(result).resolves.toEqual({ messages: [] })
    expect(pendingCount(client)).toBe(0)
    client.disconnect()
  })

  it('reports the socket generation only after a request frame is sent', async () => {
    const client = new RpcClient()
    const onSent = vi.fn()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = callOnReadySocket(client, 'chat.history', {}, { onSent })
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as { id: string }

    expect(onSent).toHaveBeenCalledOnce()
    expect(onSent).toHaveBeenCalledWith(expect.any(Number))

    socket.receive({ type: 'res', id: request.id, ok: true, payload: {} })
    await expect(result).resolves.toEqual({})
    client.disconnect()
  })

  it('generation-fences request sends and exposes the current connection generation', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const generation = client.connectionGeneration
    const sentBefore = socket.sent.length

    const stale = callOnReadySocket(client,
      'sessions.messages.unsubscribe',
      { key: 'session-a' },
      { expectedGeneration: generation - 1 },
    ).catch((error: unknown) => error)

    await expect(stale).resolves.toMatchObject({
      code: 'RPC_TRANSPORT_ERROR',
      accepted: false,
      message: expect.stringContaining('Connection generation changed'),
    })
    expect(socket.sent).toHaveLength(sentBefore)

    const current = callOnReadySocket(client,
      'sessions.messages.unsubscribe',
      { key: 'session-a' },
      { expectedGeneration: generation },
    )
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as { id: string }
    socket.receive({ type: 'res', id: request.id, ok: true, payload: {} })
    await expect(current).resolves.toEqual({})
    client.disconnect()
  })

  it('only performs consistency recovery for the generation that requested it', async () => {
    const client = new RpcClient()
    const diagnostics: Array<Record<string, unknown>> = []
    client.on('_transport', (detail: unknown) => {
      diagnostics.push(detail as Record<string, unknown>)
    })
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    const firstGeneration = client.connectionGeneration

    expect(client.recoverConnectionGeneration(firstGeneration, 'lease cleanup failed')).toBe(true)
    expect(firstSocket.readyState).toBe(MockWebSocket.CLOSED)
    expect(diagnostics).toContainEqual(expect.objectContaining({
      phase: 'retire',
      generation: firstGeneration,
      reason: 'generation_consistency_recovery',
    }))

    await vi.advanceTimersByTimeAsync(500)
    const replacement = MockWebSocket.instances[1]
    establishConnection(replacement)

    expect(client.connectionGeneration).not.toBe(firstGeneration)
    expect(client.recoverConnectionGeneration(firstGeneration, 'stale cleanup')).toBe(false)
    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('rejects a bounded call with a typed timeout and ignores a late response', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = callOnReadySocket(client,
      'chat.history',
      { sessionKey: 'session-1' },
      { timeoutMs: 25 }
    )
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as {
      type: string
      id: string
      method: string
      params: Record<string, unknown>
    }
    const caught = result.catch((error: unknown) => error)

    expect(request).toEqual({
      type: 'req',
      id: request.id,
      method: 'chat.history',
      params: { sessionKey: 'session-1' },
    })
    await vi.advanceTimersByTimeAsync(25)

    const error = await caught
    expect(error).toBeInstanceOf(RpcTimeoutError)
    expect(error).toMatchObject({
      name: 'RpcTimeoutError',
      code: 'RPC_TIMEOUT',
      method: 'chat.history',
      timeoutMs: 25,
    })
    expect(pendingCount(client)).toBe(0)

    socket.receive({ type: 'res', id: request.id, ok: true, payload: 'late' })
    expect(pendingCount(client)).toBe(0)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    client.disconnect()
  })

  it('rejects an in-flight call with a typed abort and removes its listener', async () => {
    const client = new RpcClient()
    const controller = new AbortController()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = callOnReadySocket(client, 'sessions.messages.snapshot', {}, { signal: controller.signal })
    const request = JSON.parse(socket.sent[socket.sent.length - 1]) as { id: string }
    const caught = result.catch((error: unknown) => error)
    controller.abort()

    const error = await caught
    expect(error).toBeInstanceOf(RpcAbortError)
    expect(error).toMatchObject({
      name: 'RpcAbortError',
      code: 'RPC_ABORTED',
      method: 'sessions.messages.snapshot',
    })
    expect(pendingCount(client)).toBe(0)

    socket.receive({ type: 'res', id: request.id, ok: true, payload: 'late' })
    expect(pendingCount(client)).toBe(0)
    client.disconnect()
  })

  it('keeps legacy reconnect abort request-local', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const generation = client.connectionGeneration
    const controller = new AbortController()
    const result = callOnReadySocket(client, 'sessions.messages.subscribe', {}, {
      signal: controller.signal, abortAction: 'reconnect',
    }).catch(error => error)
    controller.abort()
    expect(await result).toBeInstanceOf(RpcAbortError)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    expect(client.connectionGeneration).toBe(generation)
    await vi.advanceTimersByTimeAsync(500)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('cleans pending calls synchronously on disconnect and socket close', async () => {
    const disconnectedClient = new RpcClient()
    disconnectedClient.connect('ws://rpc.test')
    const disconnectResult = callOnReadySocket(disconnectedClient, 'chat.history')
    const disconnectError = disconnectResult.catch((error: unknown) => error)

    disconnectedClient.disconnect()

    await expect(disconnectError).resolves.toMatchObject({ message: 'Disconnected' })
    expect(pendingCount(disconnectedClient)).toBe(0)

    const closedClient = new RpcClient()
    closedClient.connect('ws://rpc.test')
    const closedSocket = MockWebSocket.instances[MockWebSocket.instances.length - 1]
    const closeResult = callOnReadySocket(closedClient, 'chat.history')
    const closeError = closeResult.catch((error: unknown) => error)

    closedSocket.close()

    await expect(closeError).resolves.toMatchObject({ message: 'Connection closed' })
    expect(pendingCount(closedClient)).toBe(0)
    closedClient.disconnect()
  })

  it('cleans a call when send throws and recycles the failed socket', async () => {
    const client = new RpcClient()
    const onSent = vi.fn()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    socket.throwOnSend = true

    const error = await callOnReadySocket(client,
      'chat.history',
      {},
      { onSent },
    ).catch((caught: unknown) => caught)

    expect(error).toMatchObject({
      message: 'send failed',
      code: 'RPC_TRANSPORT_ERROR',
      accepted: false,
    })
    expect(onSent).not.toHaveBeenCalled()
    expect(pendingCount(client)).toBe(0)
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)

    await vi.advanceTimersByTimeAsync(500)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('keeps a timed-out request local and delivers its sibling on the same socket', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const generation = client.connectionGeneration
    const sibling = callOnReadySocket(client, 'sessions.messages.snapshot')
    const siblingId = JSON.parse(socket.sent[socket.sent.length - 1]).id
    const result = callOnReadySocket(client, 'chat.history', {}, {
      timeoutMs: 25, timeoutAction: 'reconnect',
    }).catch(error => error)
    const timedId = JSON.parse(socket.sent[socket.sent.length - 1]).id
    await vi.advanceTimersByTimeAsync(25)
    expect(await result).toBeInstanceOf(RpcTimeoutError)
    expect(client.connectionGeneration).toBe(generation)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    socket.receive({ type: 'res', id: timedId, ok: true, payload: 'late' })
    expect(pendingCount(client)).toBe(1)
    socket.receive({ type: 'res', id: siblingId, ok: true, payload: 'current' })
    await expect(sibling).resolves.toBe('current')
    client.disconnect()
  })

  it('keeps four session requests on the shared socket when an advertised optional read times out', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket, {
      concurrent_optional_read_methods: ['sessions.list'],
    })

    const sessionKeys = ['session-a', 'session-b', 'session-c', 'session-d']
    const sessionRequests = sessionKeys.map(key => callOnReadySocket(client,
      'sessions.messages.snapshot',
      { key },
    ))
    const optionalRead = callOnReadySocket(client, 'sessions.list', {}, {
      timeoutMs: 25,
      timeoutAction: 'reconnect',
    }).catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(25)

    await expect(optionalRead).resolves.toBeInstanceOf(RpcTimeoutError)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    expect(MockWebSocket.instances).toHaveLength(1)

    const snapshotFrames = socket.sent
      .map(frame => JSON.parse(frame) as { id?: string; method?: string; params?: { key?: string } })
      .filter(frame => frame.method === 'sessions.messages.snapshot')
    expect(snapshotFrames.map(frame => frame.params?.key)).toEqual(sessionKeys)
    for (const frame of snapshotFrames) {
      socket.receive({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: { key: frame.params?.key },
      })
    }
    await expect(Promise.all(sessionRequests)).resolves.toEqual(
      sessionKeys.map(key => ({ key })),
    )
    client.disconnect()
  })

  it('supports typed timeout and abort termination while waiting for a connection', async () => {
    const timeoutClient = new RpcClient()
    timeoutClient.connect('ws://rpc.test')
    const timedWait = timeoutClient
      .ready(25)
      .catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(25)
    const timeoutError = await timedWait
    expect(timeoutError).toBeInstanceOf(RpcTimeoutError)
    expect(timeoutError).toMatchObject({
      code: 'RPC_TIMEOUT',
      method: 'ready',
      timeoutMs: 25,
    })
    timeoutClient.disconnect()

    const abortClient = new RpcClient()
    const controller = new AbortController()
    abortClient.connect('ws://rpc.test')
    const abortedWait = abortClient
      .ready(30_000, controller.signal)
      .catch((error: unknown) => error)
    controller.abort()

    const abortError = await abortedWait
    expect(abortError).toBeInstanceOf(RpcAbortError)
    expect(abortError).toMatchObject({
      code: 'RPC_ABORTED',
      method: 'ready',
    })
    abortClient.disconnect()
  })

  it('rejects an already-aborted connection wait even when the socket is connected', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    establishConnection(MockWebSocket.instances[0])
    const controller = new AbortController()
    controller.abort()

    await expect(
      client.ready(
        30_000,
        controller.signal,
        { abortAction: 'reconnect' },
      ),
    ).rejects.toBeInstanceOf(RpcAbortError)
    expect(client.state).toBe('connected')
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('does not let an old ready waiter cancel a replacement handshake', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const original = MockWebSocket.instances[0]
    establishConnection(original)
    original.close()
    const wait = client.ready(525, undefined, { timeoutAction: 'reconnect' }).catch(error => error)
    await vi.advanceTimersByTimeAsync(500)
    const replacement = MockWebSocket.instances[1]
    const generation = client.connectionGeneration
    await vi.advanceTimersByTimeAsync(25)
    expect(await wait).toBeInstanceOf(RpcTimeoutError)
    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(client.connectionGeneration).toBe(generation)
    establishConnection(replacement)
    await expect(client.ready()).resolves.toBeUndefined()
    client.disconnect()
  })

  it('retires a connection that never receives a challenge and uses normal backoff', async () => {
    const client = new RpcClient()
    const diagnostics: Array<Record<string, unknown>> = []
    client.on('_transport', (detail: unknown) => {
      diagnostics.push(detail as Record<string, unknown>)
    })
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]

    await vi.advanceTimersByTimeAsync(14_999)
    expect(firstSocket.readyState).toBe(MockWebSocket.OPEN)
    await vi.advanceTimersByTimeAsync(1)

    expect(firstSocket.readyState).toBe(MockWebSocket.CLOSED)
    expect(client.state).toBe('disconnected')
    expect(diagnostics).toContainEqual(expect.objectContaining({
      phase: 'watchdog_timeout',
      reason: 'connect_challenge_timeout',
    }))
    expect(diagnostics).toContainEqual(expect.objectContaining({
      phase: 'reconnect_scheduled',
      reconnectAttempt: 1,
      delay: 500,
    }))

    await vi.advanceTimersByTimeAsync(499)
    expect(MockWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('retires a connection that sends connect but never receives hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    firstSocket.receive({ type: 'event', event: 'connect.challenge' })

    await vi.advanceTimersByTimeAsync(44_999)
    expect(firstSocket.readyState).toBe(MockWebSocket.OPEN)
    await vi.advanceTimersByTimeAsync(1)

    expect(firstSocket.readyState).toBe(MockWebSocket.CLOSED)
    expect(client.state).toBe('disconnected')
    await vi.advanceTimersByTimeAsync(499)
    expect(MockWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('does not let a stale challenge watchdog close a connecting replacement', async () => {
    const client = new RpcClient()
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout')
      .mockImplementation(() => undefined)
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    const firstGeneration = client.connectionGeneration

    await vi.advanceTimersByTimeAsync(5_000)
    expect(client.recoverConnectionGeneration(firstGeneration, 'replace for test')).toBe(true)
    expect(firstSocket.readyState).toBe(MockWebSocket.CLOSED)
    await vi.advanceTimersByTimeAsync(500)

    const replacement = MockWebSocket.instances[1]
    expect(replacement).toBeDefined()
    expect(client.state).toBe('connecting')
    await vi.advanceTimersByTimeAsync(10_000)

    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(client.state).toBe('connecting')
    clearTimeoutSpy.mockRestore()
    establishConnection(replacement)
    expect(client.state).toBe('connected')
    client.disconnect()
  })

  it('emits redacted transport phase diagnostics with close metadata and conn id', () => {
    const client = new RpcClient()
    const diagnostics: Array<Record<string, unknown>> = []
    client.on('_transport', (detail: unknown) => {
      diagnostics.push(detail as Record<string, unknown>)
    })
    client.connect('ws://secret-host/private-path', 'secret-token')
    const socket = MockWebSocket.instances[0]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    socket.receive({
      protocol: 3,
      server: { version: 'test', conn_id: 'conn-test-1' },
      auth: { principal: { authenticated: true } },
      policy: { tick_interval_ms: 30_000 },
    })
    socket.close(1012, 'service_restart')

    expect(diagnostics.map(item => item.phase)).toEqual([
      'connect_start',
      'challenge',
      'hello',
      'close',
      'reconnect_scheduled',
    ])
    expect(diagnostics).toContainEqual(expect.objectContaining({
      phase: 'hello',
      connId: 'conn-test-1',
    }))
    expect(diagnostics).toContainEqual(expect.objectContaining({
      phase: 'close',
      code: 1012,
      reason: 'service_restart',
      wasClean: false,
    }))
    expect(JSON.stringify(diagnostics)).not.toContain('secret-host')
    expect(JSON.stringify(diagnostics)).not.toContain('secret-token')
    client.disconnect()
  })

  it('isolates _hello listener failures and continues connection maintenance', async () => {
    const client = new RpcClient()
    const error = new Error('hello listener failed')
    const throwingListener = vi.fn(() => { throw error })
    const siblingListener = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    client.on('_hello', throwingListener)
    client.on('_hello', siblingListener)
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    expect(() => establishConnection(socket)).not.toThrow()
    expect(throwingListener).toHaveBeenCalledOnce()
    expect(siblingListener).toHaveBeenCalledOnce()
    expect(consoleError).toHaveBeenCalledWith('[rpc] "_hello" listener failed', error)

    await vi.advanceTimersByTimeAsync(55_000)
    expect(socket.sent).toContain('{"type":"ping"}')
    socket.close()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('isolates event listener failures and still notifies siblings and wildcard listeners', () => {
    const client = new RpcClient()
    const error = new Error('event listener failed')
    const wildcardError = new Error('wildcard listener failed')
    const siblingListener = vi.fn()
    const wildcardListener = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    client.on('demo.event', () => { throw error })
    client.on('demo.event', siblingListener)
    client.on('*', () => { throw wildcardError })
    client.on('*', wildcardListener)

    expect(() => socket.receive({
      type: 'event',
      event: 'demo.event',
      payload: { ok: true },
      meta: { source: 'test' },
    })).not.toThrow()
    expect(siblingListener).toHaveBeenCalledWith({ ok: true }, { source: 'test' })
    expect(wildcardListener).toHaveBeenCalledWith(
      'demo.event',
      { ok: true },
      { source: 'test' },
    )
    expect(consoleError).toHaveBeenCalledWith('[rpc] "demo.event" listener failed', error)
    expect(consoleError).toHaveBeenCalledWith('[rpc] "*" listener failed', wildcardError)
    client.disconnect()
  })

  it('isolates _state listener failures so normal close still reconnects', async () => {
    const client = new RpcClient()
    const error = new Error('state listener failed')
    const siblingListener = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    client.on('_state', () => { throw error })
    client.on('_state', siblingListener)

    expect(() => socket.close()).not.toThrow()
    expect(client.state).toBe('disconnected')
    expect(siblingListener).toHaveBeenCalledWith('disconnected')
    expect(consoleError).toHaveBeenCalledWith('[rpc] "_state" listener failed', error)

    await vi.advanceTimersByTimeAsync(499)
    expect(MockWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('isolates _gap listener failures so sequence gaps still close and reconnect', async () => {
    const client = new RpcClient()
    const error = new Error('gap listener failed')
    const siblingListener = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    client.on('_gap', () => { throw error })
    client.on('_gap', siblingListener)
    socket.receive({ type: 'event', event: 'demo.event', seq: 1 })

    expect(() => socket.receive({ type: 'event', event: 'demo.event', seq: 3 })).not.toThrow()
    expect(siblingListener).toHaveBeenCalledWith({
      expected: 2,
      actual: 3,
      event: 'demo.event',
    })
    expect(consoleError).toHaveBeenCalledWith('[rpc] "_gap" listener failed', error)
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)

    await vi.advanceTimersByTimeAsync(499)
    expect(MockWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('keeps one capped retry after more than twenty failures', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    for (let attempt = 0; attempt < 22; attempt++) {
      MockWebSocket.instances[MockWebSocket.instances.length - 1].close()
      const delay = Math.min(15_000, 500 * 2 ** attempt)
      await vi.advanceTimersByTimeAsync(delay - 1)
      expect(MockWebSocket.instances).toHaveLength(attempt + 1)
      await vi.advanceTimersByTimeAsync(1)
      expect(MockWebSocket.instances).toHaveLength(attempt + 2)
    }
    establishConnection(MockWebSocket.instances[MockWebSocket.instances.length - 1])
    expect(client.state).toBe('connected')
    client.disconnect()
  })

  it('does not replace a healthy socket for the same connection intent', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    client.connect('ws://rpc.test')
    client.ensureConnected()
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it('only resets backoff after a stable Hello, not a flapping Hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    MockWebSocket.instances[0].close()
    await vi.advanceTimersByTimeAsync(500)
    establishConnection(MockWebSocket.instances[1])
    MockWebSocket.instances[1].close()
    await vi.advanceTimersByTimeAsync(999)
    expect(MockWebSocket.instances).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    const stable = MockWebSocket.instances[2]
    establishConnection(stable)
    await vi.advanceTimersByTimeAsync(30_000)
    stable.close()
    await vi.advanceTimersByTimeAsync(499)
    expect(MockWebSocket.instances).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(4)
    client.disconnect()
  })

  it.each(['online', 'pageshow', 'visibilitychange', 'resume'])(
    'accelerates one recovery after %s without resetting retry streak',
    async signal => {
      const client = new RpcClient()
      client.connect('ws://rpc.test')
      MockWebSocket.instances[0].close()
      await vi.advanceTimersByTimeAsync(500)
      MockWebSocket.instances[1].close()
      const target = ['visibilitychange', 'resume'].includes(signal) ? document : window
      target.dispatchEvent(new Event(signal))
      await vi.advanceTimersByTimeAsync(100)
      expect(MockWebSocket.instances).toHaveLength(3)
      MockWebSocket.instances[2].close()
      await vi.advanceTimersByTimeAsync(1_999)
      expect(MockWebSocket.instances).toHaveLength(3)
      await vi.advanceTimersByTimeAsync(1)
      expect(MockWebSocket.instances).toHaveLength(4)
      client.disconnect()
    },
  )

  it('coalesces browser wake signals and keeps a healthy pong connection', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)

    window.dispatchEvent(new Event('online'))
    window.dispatchEvent(new Event('pageshow'))
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(5_000)

    expect(socket.sent.filter(frame => frame === '{"type":"ping"}')).toHaveLength(1)
    socket.receive({ type: 'pong' })
    await vi.advanceTimersByTimeAsync(3_000)

    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('keeps a disconnected wake replacement alive after its matching hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    firstSocket.close()

    MockWebSocket.initialReadyState = MockWebSocket.CONNECTING
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(100)

    const replacement = MockWebSocket.instances[1]
    expect(replacement).toBeDefined()
    expect(client.state).toBe('connecting')
    replacement.readyState = MockWebSocket.OPEN
    establishConnection(replacement)

    await vi.advanceTimersByTimeAsync(3_001)

    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(client.state).toBe('connected')
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('leaves a connecting wake replacement to its challenge watchdog', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    firstSocket.close()

    MockWebSocket.initialReadyState = MockWebSocket.CONNECTING
    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(100)

    const replacement = MockWebSocket.instances[1]
    expect(replacement).toBeDefined()
    firstSocket.receive({ protocol: 3, policy: { tick_interval_ms: 30_000 } })

    await vi.advanceTimersByTimeAsync(3_000)

    expect(replacement.readyState).toBe(MockWebSocket.CONNECTING)
    expect(client.state).toBe('connecting')

    await vi.advanceTimersByTimeAsync(12_000)

    expect(replacement.readyState).toBe(MockWebSocket.CLOSED)
    expect(client.state).not.toBe('connected')
    client.disconnect()
  })

  it('handles a duplicate hello without rearming or repeating connection recovery', async () => {
    const client = new RpcClient()
    const helloHandler = vi.fn()
    client.on('_hello', helloHandler)
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    firstSocket.close()

    MockWebSocket.initialReadyState = MockWebSocket.CONNECTING
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(100)

    const replacement = MockWebSocket.instances[1]
    replacement.readyState = MockWebSocket.OPEN
    establishConnection(replacement)
    replacement.receive({ protocol: 3, policy: { tick_interval_ms: 30_000 } })

    await vi.advanceTimersByTimeAsync(3_001)

    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(client.state).toBe('connected')
    expect(helloHandler).toHaveBeenCalledTimes(2)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('cleans a connecting handshake deadline on explicit disconnect', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    firstSocket.close()

    MockWebSocket.initialReadyState = MockWebSocket.CONNECTING
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(100)

    const replacement = MockWebSocket.instances[1]
    expect(replacement.readyState).toBe(MockWebSocket.CONNECTING)
    client.disconnect()
    await vi.advanceTimersByTimeAsync(15_001)

    expect(replacement.readyState).toBe(MockWebSocket.CLOSED)
    expect(client.state).toBe('disconnected')
    expect(MockWebSocket.instances).toHaveLength(2)
  })

  it('probes the connection when a hidden page becomes visible', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)

    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(5_000)

    expect(socket.sent).toContain('{"type":"ping"}')
    socket.receive({ type: 'pong' })
    client.disconnect()
  })

  it('tolerates a short wake pause, then recovers a persistent half-open socket', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket, { transport_probe_nonce: true })
    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(5_000)
    const ping = JSON.parse(socket.sent[socket.sent.length - 1])
    expect(ping).toMatchObject({ type: 'ping', nonce: expect.any(String) })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(client.health).toBe('suspect')
    socket.receive({ type: 'event', event: 'tick' })
    await vi.advanceTimersByTimeAsync(20_000)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
    await vi.advanceTimersByTimeAsync(500)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('does not accept a wrong nonce or duplicate Hello as control recovery', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket, { transport_probe_nonce: true })
    await vi.advanceTimersByTimeAsync(30_000)
    const ping = JSON.parse(socket.sent[socket.sent.length - 1])
    socket.receive({ type: 'pong', nonce: 'wrong' })
    socket.receive({ protocol: 3, policy: {} })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(client.health).toBe('suspect')
    await vi.advanceTimersByTimeAsync(1_000)
    const current = JSON.parse(socket.sent[socket.sent.length - 1])
    expect(current.nonce).not.toBe(ping.nonce)
    socket.receive({ type: 'pong', nonce: current.nonce })
    expect(client.health).toBe('healthy')
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    client.disconnect()
  })

  it('does not let an old wake deadline retire the gateway replacement', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)

    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(100)
    firstSocket.close()
    await vi.advanceTimersByTimeAsync(1_000)

    const replacement = MockWebSocket.instances[1]
    establishConnection(replacement)
    await vi.advanceTimersByTimeAsync(2_000)

    expect(replacement.readyState).toBe(MockWebSocket.OPEN)
    expect(client.state).toBe('connected')
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('leaves handshake ownership with the client when a page waiter expires', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    const wait = client.ready(25, undefined, { timeoutAction: 'reconnect' }).catch(error => error)
    await vi.advanceTimersByTimeAsync(25)
    expect(await wait).toBeInstanceOf(RpcTimeoutError)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    expect(MockWebSocket.instances).toHaveLength(1)
    establishConnection(socket)
    await expect(client.ready()).resolves.toBeUndefined()
    client.disconnect()
  })

  it('never sends business requests before authenticated Hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    await expect(client.call('chat.send', {})).rejects.toMatchObject({ accepted: false })
    expect(MockWebSocket.instances[0].sent).toEqual([])
    client.disconnect()
  })

  it('blocks automatic recovery after credentialed guest downgrade until an explicit connection intent', async () => {
    const client = new RpcClient()
    const blocked = vi.fn()
    client.on('_blocked', blocked)
    client.connect('ws://rpc.test', 'bad-token')
    const old = MockWebSocket.instances[0]
    const lateHello = old.onmessage
    establishConnection(old)
    expect(client.lifecycle).toBe('blocked')
    client.ensureConnected()
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(blocked).toHaveBeenCalledOnce()
    client.connect('ws://rpc.test', 'new-token')
    const socket = MockWebSocket.instances[1]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    lateHello?.({ data: JSON.stringify({ protocol: 3, auth: { principal: { authenticated: true } } }) } as MessageEvent)
    expect(client.state).toBe('connecting')
    socket.receive({ protocol: 3, auth: { principal: { authenticated: true, isOwner: false } } })
    expect(client.state).toBe('connected')
    client.disconnect()
  })

  it('requires owner only for owner intent and installs identity before ready', () => {
    const client = new RpcClient()
    const trace: string[] = []
    client.on('_hello', () => trace.push('identity'))
    client.on('_state', state => { if (state === 'connected') trace.push('connected') })
    client.connect('ws://rpc.test', 'desktop-token', { authentication: 'owner', key: 'profile:runtime' })
    let socket = MockWebSocket.instances[0]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    socket.receive({ protocol: 3, auth: { principal: { authenticated: true, isOwner: false } } })
    expect(client.lifecycle).toBe('blocked')
    expect(trace).toEqual([])
    client.connect('ws://rpc.test', 'replacement-token', { authentication: 'owner', key: 'profile:runtime' })
    socket = MockWebSocket.instances[1]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    socket.receive({ protocol: 3, auth: { principal: { authenticated: true, isOwner: true } } })
    expect(trace).toEqual(['identity', 'connected'])
    client.disconnect()
  })

  it.each(['UNAUTHORIZED', 'INVALID_REQUEST'])('blocks permanent handshake %s without a storm', async code => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    socket.receive({ type: 'event', event: 'connect.challenge' })
    const request = JSON.parse(socket.sent[socket.sent.length - 1])
    socket.receive({ type: 'res', id: request.id, ok: false, error: { code } })
    expect(client.lifecycle).toBe('blocked')
    await vi.advanceTimersByTimeAsync(120_000)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('keeps scope rejection request-local after valid Hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const request = client.call('settings.ownerOnly').catch(error => error)
    const id = JSON.parse(socket.sent[socket.sent.length - 1]).id
    socket.receive({ type: 'res', id, ok: false, error: { code: 'UNAUTHORIZED' } })
    expect(await request).toMatchObject({ code: 'UNAUTHORIZED' })
    expect(client.lifecycle).toBe('connected')
    client.disconnect()
  })

  it('treats scheduler suspension as a fresh probe opportunity', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket, { transport_probe_nonce: true })
    await vi.advanceTimersByTimeAsync(30_000)
    vi.setSystemTime(Date.now() + 300_000)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    await vi.advanceTimersByTimeAsync(5_000)
    const probe = JSON.parse(socket.sent[socket.sent.length - 1])
    socket.receive({ type: 'pong', nonce: probe.nonce })
    expect(client.health).toBe('healthy')
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('retains the socket after registered successful gap recovery', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const recover = vi.fn(async () => true)
    client.onGap(recover)
    socket.receive({ type: 'event', event: 'demo', seq: 1 })
    socket.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    expect(recover).toHaveBeenCalledOnce()
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    client.disconnect()
  })

  it('retries a failed gap recovery on the same healthy socket until it really succeeds', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const generation = client.connectionGeneration
    const recover = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true)
    client.onGap(recover)
    socket.receive({ type: 'event', event: 'demo', seq: 1 })
    socket.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    expect(recover).toHaveBeenCalledOnce()
    expect(client.state).toBe('connected')
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    await vi.advanceTimersByTimeAsync(999)
    expect(recover).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(recover).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(recover).toHaveBeenCalledTimes(2)
    expect(client.connectionGeneration).toBe(generation)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('retains modern-client gap responsibility before a domain owner mounts and still delivers flow receipts', async () => {
    const client = new RpcClient()
    client.enableConsumptionFlow()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const recoverAttempt = vi.spyOn(client, 'recoverGap')
    const receive = vi.fn()
    client.on('demo', receive)
    socket.receive({ type: 'event', event: 'demo', seq: 1 })
    socket.receive({ type: 'event', event: 'demo', seq: 3, meta: { flow: { delivery_epoch: 'epoch', delivery_id: 1 } } })
    await vi.advanceTimersByTimeAsync(2050)
    expect(recoverAttempt).toHaveBeenCalledTimes(3)
    expect(receive).toHaveBeenCalledTimes(2)
    expect(socket.readyState).toBe(MockWebSocket.OPEN)
    const recoverOwner = vi.fn(async () => true)
    client.onGap(recoverOwner)
    await vi.advanceTimersByTimeAsync(1000)
    expect(recoverOwner).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(2000)
    expect(recoverOwner).toHaveBeenCalledOnce()
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('coalesces consecutive gaps into one active recovery and one bounded retry timer', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const baselineTimers = vi.getTimerCount()
    let finish!: (value: boolean) => void
    const recover = vi.fn().mockReturnValueOnce(new Promise<boolean>(resolve => { finish = resolve })).mockResolvedValue(true)
    client.onGap(recover)
    for (const seq of [1, 3, 5, 7]) socket.receive({ type: 'event', event: 'demo', seq })
    await vi.advanceTimersByTimeAsync(5000)
    expect(recover).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(baselineTimers)
    finish(false)
    await vi.advanceTimersByTimeAsync(0)
    expect(vi.getTimerCount()).toBe(baselineTimers + 1)
    for (const seq of [9, 11]) socket.receive({ type: 'event', event: 'demo', seq })
    expect(vi.getTimerCount()).toBe(baselineTimers + 1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(recover).toHaveBeenCalledTimes(2)
    expect(recover).toHaveBeenLastCalledWith({ expected: 10, actual: 11, event: 'demo' })
    await vi.advanceTimersByTimeAsync(3000)
    expect(recover).toHaveBeenCalledTimes(2)
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('clears pending gap retries on explicit stop', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)
    const recover = vi.fn(async () => false)
    client.onGap(recover)
    socket.receive({ type: 'event', event: 'demo', seq: 1 })
    socket.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    client.disconnect()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10000)
    expect(recover).toHaveBeenCalledOnce()
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it('ignores late gap recovery failure after a socket replacement', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const old = MockWebSocket.instances[0]
    establishConnection(old)
    let finish!: (value: boolean) => void
    const recover = vi.fn().mockReturnValueOnce(new Promise<boolean>(resolve => { finish = resolve })).mockResolvedValue(true)
    client.onGap(recover)
    old.receive({ type: 'event', event: 'demo', seq: 1 })
    old.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    old.close()
    await vi.advanceTimersByTimeAsync(500)
    const current = MockWebSocket.instances[1]
    establishConnection(current)
    finish(false)
    await vi.advanceTimersByTimeAsync(3000)
    expect(recover).toHaveBeenCalledOnce()
    expect(current.readyState).toBe(MockWebSocket.OPEN)
    current.receive({ type: 'event', event: 'demo', seq: 1 })
    current.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    expect(recover).toHaveBeenCalledTimes(2)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('clears a scheduled gap retry when a remote close replaces the socket', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const old = MockWebSocket.instances[0]
    establishConnection(old)
    const recover = vi.fn(async () => false)
    client.onGap(recover)
    old.receive({ type: 'event', event: 'demo', seq: 1 })
    old.receive({ type: 'event', event: 'demo', seq: 3 })
    await vi.advanceTimersByTimeAsync(0)
    old.close()
    await vi.advanceTimersByTimeAsync(500)
    establishConnection(MockWebSocket.instances[1])
    await vi.advanceTimersByTimeAsync(3000)
    expect(recover).toHaveBeenCalledOnce()
    expect(client.state).toBe('connected')
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('does not dispatch a queued obsolete gap read onto a new connection intent', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const old = MockWebSocket.instances[0]
    establishConnection(old)
    const recover = vi.fn(async () => true)
    client.onGap(recover)
    old.receive({ type: 'event', event: 'demo', seq: 1 })
    old.receive({ type: 'event', event: 'demo', seq: 3 })
    client.connect('ws://replacement.test')
    establishConnection(MockWebSocket.instances[1])
    await vi.advanceTimersByTimeAsync(3000)
    expect(recover).not.toHaveBeenCalled()
    expect(client.state).toBe('connected')
    client.disconnect()
  })

  it('does not claim consumption from observers or unfinished domain work', async () => {
    const client = new RpcClient()
    client.on('demo', vi.fn())
    await expect(client.consumeEvent('demo', {}, {})).rejects.toThrow('No consumption owner')
    let finish!: (result: 'applied' | 'dirty') => void
    client.onConsumedEvent('demo', () => new Promise(resolve => { finish = resolve }))
    let completed = false
    const result = client.consumeEvent('demo', {}, {}).then(value => { completed = true; return value })
    await Promise.resolve()
    expect(completed).toBe(false)
    finish('applied')
    await expect(result).resolves.toBe('applied')
  })
})
