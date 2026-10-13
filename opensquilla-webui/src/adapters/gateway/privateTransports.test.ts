// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { RpcClient } from '@/lib/rpc'
import { TRANSPORT_SESSION_FLOW_V2_METHOD } from '@/contracts/transportFlowCapabilities'
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
  it('reflects read-v2 methods negotiated after transport construction and on reconnect', () => {
    const advertised = new Set<string>()
    const rpcSource = { ...source(), hasRpcMethod: vi.fn((method: string) => advertised.has(method)) }
    const { rpc } = createPrivateGatewayTransports(rpcSource)

    expect(rpc.sessionReadV2).toBe(false)
    for (const method of [
      'sessions.read.open.v2', 'sessions.read.state.v2', 'sessions.read.install.v2',
      'sessions.read.close.v2', 'sessions.history.page.v2',
    ]) advertised.add(method)
    expect(rpc.sessionReadV2).toBe(true)
    advertised.delete('sessions.read.install.v2')
    expect(rpc.sessionReadV2).toBe(false)
    advertised.add('sessions.read.install.v2')
    expect(rpc.sessionReadV2).toBe(true)
  })

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

  it('retains the source receiver for fallback delivery control', async () => {
    const rpcSource = { ...source(), acknowledgeDelivery: vi.fn(), resumeFlow: vi.fn() }
    const { rpc } = createPrivateGatewayTransports(rpcSource)
    await rpc.acknowledgeDelivery?.({ delivery_epoch: 'epoch', delivery_id: 1 })
    await rpc.resumeFlow?.({
      key: 'alpha', snapshot_id: 'snapshot', sync_revision: 'revision',
      stream_generation: 'stream', stream_seq: 1,
    })
    expect(rpcSource.acknowledgeDelivery.mock.contexts).toEqual([rpcSource])
    expect(rpcSource.resumeFlow.mock.contexts).toEqual([rpcSource])
  })

  it('requires negotiated flow before exposing advertised snapshot recovery methods', () => {
    const handlers = new Map<string, RpcEventHandler>()
    const rpcSource = {
      ...source(),
      hasRpcMethod: vi.fn(() => true),
      on: vi.fn((event: string, handler: RpcEventHandler) => {
        handlers.set(event, handler)
        return vi.fn()
      }),
      enableConsumptionFlow: vi.fn(),
      consumeEvent: vi.fn(async () => 'applied' as const),
      recoverGap: vi.fn(async () => true),
    }
    const { rpc } = createPrivateGatewayTransports(rpcSource)
    const recoveryMethods = ['sessions.messages.resume', 'sessions.messages.snapshot.release']
    // Both server kill switches omit the flow policy while methods remain advertised.
    for (const transport_flow of [undefined, null]) {
      handlers.get('_hello')?.({ policy: { transport_flow } })
      for (const method of recoveryMethods) expect(rpc.supports(method)).toBe(false)
      expect(rpc.supports('sessions.messages.snapshot.read')).toBe(true)
    }
    handlers.get('_hello')?.({ policy: { transport_flow: {
      delivery_epoch: 'current', window_frames: 128, window_bytes: 4 * 1024 * 1024,
    } } })
    for (const method of recoveryMethods) expect(rpc.supports(method)).toBe(true)
    handlers.get('_state')?.('disconnected')
    for (const method of recoveryMethods) expect(rpc.supports(method)).toBe(false)
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

class Socket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: Socket[] = []
  readonly sent: string[] = []
  readyState = Socket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null
  constructor(readonly url: string) { Socket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() {
    this.readyState = Socket.CLOSED
    this.onclose?.({ code: 1000, wasClean: true } as CloseEvent)
  }
  receive(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent) }
}

function hello(socket: Socket, epoch = 'connection-1') {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    type: 'hello-ok', protocol: 3,
    server: { version: 'test', conn_id: epoch },
    features: { methods: [], events: [] }, snapshot: {},
    auth: { principal: {
      role: 'operator', scopes: ['operator.read', 'operator.write'], capabilities: [],
      isOwner: true, authenticated: false, authState: 'authenticated', tokenPublicId: null,
    } },
    policy: { transport_flow: {
      delivery_epoch: epoch, window_frames: 128, window_bytes: 4194304,
      capability: 'transport.session-flow.v2',
    } },
  })
}

function retireResult() {
  return { lane_retire: {
    connection_epoch: 'connection-1', subscription_epoch: 'old-alpha',
    retire_token: 'retire-old-alpha', final_published_id: 0,
  } }
}

function frames(socket: Socket, method: string) {
  return socket.sent.map(frame => JSON.parse(frame)).filter(frame => frame.method === method)
}

function confirmRetire(socket: Socket) {
  const updates = frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)
  const update = updates[updates.length - 1]
  socket.receive({ type: 'res', id: update.id, ok: true, payload: {
    connection_epoch: update.params.connection_epoch,
    consumed: [], staged_recovery: [], discarded_lanes: update.params.discarded_lanes ?? [],
  } })
}

describe('orphan lane retirement over RpcClient', () => {
  const clients: RpcClient[] = []

  function connect() {
    const client = new RpcClient()
    clients.push(client)
    const handlers = new Map<string, RpcEventHandler>()
    const source = {
      get connectionGeneration() { return client.connectionGeneration },
      call: <T = unknown>(...args: Parameters<RpcClient['call']>) => client.call(...args) as Promise<T>,
      on: (event: string, handler: RpcEventHandler) => {
        handlers.set(event, handler)
        return client.on(event, handler)
      },
      onConsumedEvent: client.onConsumedEvent.bind(client),
      consumeEvent: client.consumeEvent.bind(client),
      recoverGap: client.recoverGap.bind(client),
      enableConsumptionFlow: client.enableConsumptionFlow.bind(client),
      ready: client.ready.bind(client),
      hasRpcMethod: () => true, hasRpcEvent: () => true,
      rememberUnsupportedMethod: () => {},
    }
    const transport = createPrivateGatewayTransports(source)
    client.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[Socket.instances.length - 1]
    hello(socket)
    return { client, socket, transport, handlers }
  }

  beforeEach(() => {
    vi.useFakeTimers()
    Socket.instances = []
    localStorage.clear()
    vi.stubGlobal('WebSocket', Socket)
  })

  afterEach(() => {
    for (const client of clients.splice(0)) client.disconnect()
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it.each(['timeout', 'abort'])('confirms a late retire receipt after local %s without replaying unsubscribe', async reason => {
    const { client, socket, transport } = connect()
    const controller = new AbortController()
    const result = transport.rpc.request('sessions.messages.unsubscribe', { key: 'alpha' }, {
      timeoutMs: 10, signal: controller.signal, timeoutAction: 'reject', abortAction: 'reject',
    }).catch(error => error)
    const request = frames(socket, 'sessions.messages.unsubscribe')[0]
    if (reason === 'abort') controller.abort()
    await vi.advanceTimersByTimeAsync(10)
    expect(await result).toBeInstanceOf(Error)

    const response = { type: 'res', id: request.id, ok: true, payload: retireResult() }
    socket.receive(response)
    socket.receive(response)
    await vi.advanceTimersByTimeAsync(0)
    const updates = frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)
    expect(updates).toHaveLength(1)
    expect(updates[0].params).toEqual({
      connection_epoch: 'connection-1', discarded_lanes: [{
        subscription_epoch: 'old-alpha', retire_token: 'retire-old-alpha', final_published_id: 0,
      }],
    })
    confirmRetire(socket)
    await vi.advanceTimersByTimeAsync(100)
    expect(frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)).toHaveLength(1)
    expect(frames(socket, 'sessions.messages.unsubscribe')).toHaveLength(1)
    expect(client.state).toBe('connected')
  })

  it('requires the complete result schema and the current generation and connection epoch', async () => {
    const { client, socket, handlers } = connect()
    const valid = retireResult()
    const { retire_token: _token, ...missingToken } = valid.lane_retire
    const malformed = [
      null, {}, valid.lane_retire,
      { ...valid, extra: true },
      { lane_retire: { ...valid.lane_retire, extra: true } },
      { lane_retire: missingToken },
      { lane_retire: { ...valid.lane_retire, final_published_id: -1 } },
      { lane_retire: { ...valid.lane_retire, final_published_id: '0' } },
      { lane_retire: { ...valid.lane_retire, connection_epoch: 'old-connection' } },
    ]
    malformed.forEach((payload, id) => socket.receive({ type: 'res', id: String(id), ok: true, payload }))
    handlers.get('_orphan_response')?.({
      generation: client.connectionGeneration - 1, payload: valid,
    })
    await vi.advanceTimersByTimeAsync(100)
    expect(frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)).toHaveLength(0)
    expect(frames(socket, 'sessions.messages.unsubscribe')).toHaveLength(0)
    expect(client.state).toBe('connected')
  })

  it('leaves an owned unsubscribe response to its caller', async () => {
    const { socket, transport } = connect()
    const result = transport.rpc.request('sessions.messages.unsubscribe', { key: 'alpha' })
    const request = frames(socket, 'sessions.messages.unsubscribe')[0]
    socket.receive({ type: 'res', id: request.id, ok: true, payload: retireResult() })
    await expect(result).resolves.toEqual(retireResult())
    await vi.advanceTimersByTimeAsync(100)
    expect(frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)).toHaveLength(0)
  })

  it('does not credit a replacement connection for an old socket response', async () => {
    const { client, socket } = connect()
    client.connect('ws://127.0.0.1:18791/ws')
    const replacement = Socket.instances[Socket.instances.length - 1]
    hello(replacement, 'connection-2')
    socket.receive({ type: 'res', id: 'late', ok: true, payload: retireResult() })
    await vi.advanceTimersByTimeAsync(100)
    expect(frames(replacement, TRANSPORT_SESSION_FLOW_V2_METHOD)).toHaveLength(0)
    expect(client.state).toBe('connected')
  })

  it('repeats only the exact retire ACK after confirmation and keeps the replacement lane consumable', async () => {
    const { socket, transport } = connect()
    const consumed = vi.fn(async () => 'applied' as const)
    transport.events.subscribeConsumed!('session.event.text_delta', consumed)
    const response = { type: 'res', id: 'late', ok: true, payload: retireResult() }
    socket.receive(response)
    await vi.advanceTimersByTimeAsync(0)
    confirmRetire(socket)
    await vi.advanceTimersByTimeAsync(0)
    socket.receive(response)
    await vi.advanceTimersByTimeAsync(0)
    const updates = frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)
    expect(updates).toHaveLength(2)
    expect(updates[1].params).toEqual(updates[0].params)
    confirmRetire(socket)
    socket.receive({
      type: 'event', event: 'session.event.text_delta', payload: { session_key: 'alpha', text_delta: 'new' },
      meta: {
        flow: { delivery_epoch: 'connection-1', delivery_id: 1 },
        session_flow_v2: { connection_epoch: 'connection-1', subscription_epoch: 'new-alpha', delivery_id: 1 },
      }, seq: 1,
    })
    await vi.advanceTimersByTimeAsync(100)
    expect(consumed).toHaveBeenCalledOnce()
    const finalUpdates = frames(socket, TRANSPORT_SESSION_FLOW_V2_METHOD)
    expect(finalUpdates[finalUpdates.length - 1].params).toEqual({
      connection_epoch: 'connection-1', consumed: [{ subscription_epoch: 'new-alpha', through_delivery_id: 1 }],
    })
    expect(frames(socket, 'sessions.messages.unsubscribe')).toHaveLength(0)
  })
})
