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
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this)
  }

  send(data: string): void {
    if (this.throwOnSend) throw new Error('send failed')
    this.sent.push(data)
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
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

describe('RpcClient', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    MockWebSocket.initialReadyState = MockWebSocket.OPEN
    localStorage.clear()
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.useFakeTimers()
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

    const frame = JSON.parse(socket.sent[0]) as {
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

    const result = client.call(
      'chat.send',
      { message: 'hello' },
      { timeoutMs: 100, timeoutAction: 'reconnect' }
    )
    const request = JSON.parse(socket.sent[0]) as { id: string }
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

    const result = client.call('chat.history', { sessionKey: 'session-1' })
    const request = JSON.parse(socket.sent[0]) as {
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
    await vi.advanceTimersByTimeAsync(120_000)
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

    const result = client.call('chat.history', {}, { onSent })
    const request = JSON.parse(socket.sent[0]) as { id: string }

    expect(onSent).toHaveBeenCalledOnce()
    expect(onSent).toHaveBeenCalledWith(expect.any(Number))

    socket.receive({ type: 'res', id: request.id, ok: true, payload: {} })
    await expect(result).resolves.toEqual({})
    client.disconnect()
  })

  it('rejects a bounded call with a typed timeout and ignores a late response', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = client.call(
      'chat.history',
      { sessionKey: 'session-1' },
      { timeoutMs: 25 }
    )
    const request = JSON.parse(socket.sent[0]) as {
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

    const result = client.call('sessions.messages.snapshot', {}, { signal: controller.signal })
    const request = JSON.parse(socket.sent[0]) as { id: string }
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

  it('can recycle the current socket when an abort requests reconnect', async () => {
    const client = new RpcClient()
    const controller = new AbortController()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = client.call(
      'sessions.messages.subscribe',
      {},
      { signal: controller.signal, abortAction: 'reconnect' }
    )
    const caught = result.catch((error: unknown) => error)
    controller.abort()

    expect(await caught).toBeInstanceOf(RpcAbortError)
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
    expect(pendingCount(client)).toBe(0)

    await vi.advanceTimersByTimeAsync(0)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('cleans pending calls synchronously on disconnect and socket close', async () => {
    const disconnectedClient = new RpcClient()
    disconnectedClient.connect('ws://rpc.test')
    const disconnectResult = disconnectedClient.call('chat.history')
    const disconnectError = disconnectResult.catch((error: unknown) => error)

    disconnectedClient.disconnect()

    await expect(disconnectError).resolves.toMatchObject({ message: 'Disconnected' })
    expect(pendingCount(disconnectedClient)).toBe(0)

    const closedClient = new RpcClient()
    closedClient.connect('ws://rpc.test')
    const closedSocket = MockWebSocket.instances[MockWebSocket.instances.length - 1]
    const closeResult = closedClient.call('chat.history')
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
    socket.throwOnSend = true

    const error = await client.call(
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

    await vi.advanceTimersByTimeAsync(0)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('recycles on timeout without letting stale socket events close the replacement', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    expect(client.state).toBe('connected')

    const sibling = client.call('sessions.messages.snapshot')
    const siblingCaught = sibling.catch((error: unknown) => error)
    const result = client.call('chat.history', {}, {
      timeoutMs: 25,
      timeoutAction: 'reconnect',
    })
    const request = JSON.parse(firstSocket.sent[firstSocket.sent.length - 1]) as { id: string }
    const staleClose = firstSocket.onclose
    const caught = result.catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(25)
    expect(await caught).toBeInstanceOf(RpcTimeoutError)
    await expect(siblingCaught).resolves.toMatchObject({
      message: 'Connection recycled after chat.history terminated',
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    })
    await vi.runOnlyPendingTimersAsync()

    const secondSocket = MockWebSocket.instances[1]
    expect(secondSocket).toBeDefined()
    establishConnection(secondSocket)
    expect(client.state).toBe('connected')

    staleClose?.()
    firstSocket.receive({ type: 'res', id: request.id, ok: true, payload: 'late' })

    expect(client.state).toBe('connected')
    expect(pendingCount(client)).toBe(0)
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
    const sessionRequests = sessionKeys.map(key => client.call(
      'sessions.messages.snapshot',
      { key },
    ))
    const optionalRead = client.call('sessions.list', {}, {
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
      .waitForConnection(25)
      .catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(25)
    const timeoutError = await timedWait
    expect(timeoutError).toBeInstanceOf(RpcTimeoutError)
    expect(timeoutError).toMatchObject({
      code: 'RPC_TIMEOUT',
      method: 'waitForConnection',
      timeoutMs: 25,
    })
    timeoutClient.disconnect()

    const abortClient = new RpcClient()
    const controller = new AbortController()
    abortClient.connect('ws://rpc.test')
    const abortedWait = abortClient
      .waitForConnection(30_000, controller.signal)
      .catch((error: unknown) => error)
    controller.abort()

    const abortError = await abortedWait
    expect(abortError).toBeInstanceOf(RpcAbortError)
    expect(abortError).toMatchObject({
      code: 'RPC_ABORTED',
      method: 'waitForConnection',
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
      client.waitForConnection(
        30_000,
        controller.signal,
        { abortAction: 'reconnect' },
      ),
    ).rejects.toBeInstanceOf(RpcAbortError)
    expect(client.state).toBe('connected')
    expect(MockWebSocket.instances).toHaveLength(1)
    client.disconnect()
  })

  it('recycles the replacement socket when a wait spans a disconnected gap', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    establishConnection(firstSocket)
    firstSocket.close()

    const timedWait = client.waitForConnection(
      1_025,
      undefined,
      { timeoutAction: 'reconnect' },
    ).catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(1_000)
    const replacement = MockWebSocket.instances[1]
    expect(replacement).toBeDefined()
    expect(client.state).toBe('connecting')

    await vi.advanceTimersByTimeAsync(25)
    await expect(timedWait).resolves.toBeInstanceOf(RpcTimeoutError)
    expect(replacement.readyState).toBe(MockWebSocket.CLOSED)

    await vi.advanceTimersByTimeAsync(1)
    const retrySocket = MockWebSocket.instances[2]
    expect(retrySocket).toBeDefined()
    establishConnection(retrySocket)
    await expect(client.waitForConnection(25)).resolves.toBeUndefined()
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

    await vi.advanceTimersByTimeAsync(999)
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

    await vi.advanceTimersByTimeAsync(999)
    expect(MockWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('reconnects with the fixed 1/2/4/8/15 second backoff and resets after hello', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')

    const delays = [1_000, 2_000, 4_000, 8_000, 15_000, 15_000]
    for (const [index, delay] of delays.entries()) {
      MockWebSocket.instances[index].close()
      await vi.advanceTimersByTimeAsync(delay - 1)
      expect(MockWebSocket.instances).toHaveLength(index + 1)
      await vi.advanceTimersByTimeAsync(1)
      expect(MockWebSocket.instances).toHaveLength(index + 2)
    }

    const recovered = MockWebSocket.instances[MockWebSocket.instances.length - 1]!
    establishConnection(recovered)
    recovered.close()
    await vi.advanceTimersByTimeAsync(999)
    expect(MockWebSocket.instances).toHaveLength(delays.length + 1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(delays.length + 2)
    client.disconnect()
  })

  it('interrupts a saturated reconnect backoff when the browser comes online', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')

    const delays = [1_000, 2_000, 4_000, 8_000, 15_000]
    for (const [index, delay] of delays.entries()) {
      MockWebSocket.instances[index].close()
      await vi.advanceTimersByTimeAsync(delay)
    }
    const saturated = MockWebSocket.instances[delays.length]
    saturated.close()

    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(99)
    expect(MockWebSocket.instances).toHaveLength(delays.length + 1)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(delays.length + 2)
    client.disconnect()
  })

  it('coalesces browser wake signals and keeps a healthy pong connection', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)

    window.dispatchEvent(new Event('online'))
    window.dispatchEvent(new Event('pageshow'))
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(100)

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

  it('does not let an old generation hello clear the replacement wake deadline', async () => {
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

  it('cleans a connecting wake deadline on explicit disconnect', async () => {
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
    await vi.advanceTimersByTimeAsync(3_001)

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
    await vi.advanceTimersByTimeAsync(100)

    expect(socket.sent).toContain('{"type":"ping"}')
    socket.receive({ type: 'pong' })
    client.disconnect()
  })

  it('replaces a half-open socket after a wake probe receives no pong', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)

    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(100)
    expect(socket.sent).toContain('{"type":"ping"}')

    await vi.advanceTimersByTimeAsync(3_000)
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
    await vi.advanceTimersByTimeAsync(1)
    expect(MockWebSocket.instances).toHaveLength(2)
    client.disconnect()
  })

  it('does not treat a duplicate hello as the pong required by an open wake probe', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]
    establishConnection(socket)

    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(100)
    expect(socket.sent).toContain('{"type":"ping"}')

    socket.receive({ protocol: 3, policy: { tick_interval_ms: 30_000 } })
    await vi.advanceTimersByTimeAsync(3_000)

    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
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

  it('retires a half-connected socket when the handshake wait times out', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const firstSocket = MockWebSocket.instances[0]
    const timedWait = client
      .waitForConnection(
        25,
        undefined,
        { timeoutAction: 'reconnect' },
      )
      .catch((error: unknown) => error)

    await vi.advanceTimersByTimeAsync(25)
    await vi.advanceTimersByTimeAsync(1)

    await expect(timedWait).resolves.toBeInstanceOf(RpcTimeoutError)
    expect(firstSocket.readyState).toBe(MockWebSocket.CLOSED)
    expect(MockWebSocket.instances).toHaveLength(2)

    const secondSocket = MockWebSocket.instances[1]
    establishConnection(secondSocket)
    await expect(client.waitForConnection(25)).resolves.toBeUndefined()
    expect(client.state).toBe('connected')
    client.disconnect()
  })
})
