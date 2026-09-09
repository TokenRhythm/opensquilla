// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPrivateGatewayTransports } from '@/adapters/gateway/privateTransports'
import { RpcClient } from './rpc'

class Socket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: Socket[] = []
  readonly sent: string[] = []
  autoPong = false
  readyState = Socket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null
  constructor(readonly url: string) { Socket.instances.push(this) }
  send(data: string) {
    this.sent.push(data)
    const frame = JSON.parse(data)
    if (this.autoPong && frame.type === 'ping') {
      queueMicrotask(() => this.receive({ type: 'pong', ...(frame.nonce ? { nonce: frame.nonce } : {}) }))
    }
  }
  close() { this.readyState = Socket.CLOSED; this.onclose?.({ code: 1000, wasClean: true } as CloseEvent) }
  receive(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent) }
}

// Mirrors OpenScopeResolver -> principal_payload for a loopback auth:none
// owner: authenticated denotes verified-token authentication, not ownership.
const localOwner = {
  role: 'operator',
  scopes: ['operator.admin', 'operator.approvals', 'operator.pairing', 'operator.proposals', 'operator.read', 'operator.write'],
  capabilities: ['host.execute', 'host.read', 'task.read', 'task.submit'],
  isOwner: true, authenticated: false, authState: 'authenticated', tokenPublicId: null,
}
const clients: RpcClient[] = []
function client() { const rpc = new RpcClient(); clients.push(rpc); return rpc }
function hello(socket: Socket, principal = localOwner, policy: Record<string, unknown> = {}) {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({ protocol: 3, auth: { principal }, policy })
}
function transports(rpc: RpcClient) {
  const source = {
    get connectionGeneration() { return rpc.connectionGeneration },
    call: <T = unknown>(...args: Parameters<RpcClient['call']>) => rpc.call(...args) as Promise<T>,
    on: rpc.on.bind(rpc),
    onConsumedEvent: rpc.onConsumedEvent.bind(rpc),
    onGap: rpc.onGap.bind(rpc),
    consumeEvent: rpc.consumeEvent.bind(rpc),
    recoverGap: rpc.recoverGap.bind(rpc),
    enableConsumptionFlow: rpc.enableConsumptionFlow.bind(rpc),
    ready: rpc.ready.bind(rpc),
    hasRpcMethod: () => true,
    hasRpcEvent: () => true,
    rememberUnsupportedMethod: () => {},
  }
  return { source, transport: createPrivateGatewayTransports(source) }
}
const flowPolicy = {
  transport_probe_nonce: true,
  transport_flow: { delivery_epoch: 'epoch-1', window_frames: 128, window_bytes: 4194304 },
}
function snapshotResult() {
  return {
    key: 'alpha', sync_revision: 'sync-1', snapshot_id: 'snapshot-1', segment_index: 0,
    segment_count: 1, byte_length: 2, encoding: 'base64-json-utf8', data: 'e30=',
    stream_generation: 'stream-1', current_stream_seq: 0, session_id: 'session-1', session_epoch: 1,
    task_id: null, delivery: { delivery_epoch: 'epoch-1', delivery_id: 1 },
  }
}
function flowUpdates(socket: Socket) {
  return socket.sent.map(frame => JSON.parse(frame)).filter(frame => frame.method === 'transport.flow.update')
}
function confirmUpdate(socket: Socket) {
  const updates = flowUpdates(socket)
  const update = updates[updates.length - 1]
  socket.receive({ type: 'res', id: update.id, ok: true, payload: {
    delivery_epoch: update.params.delivery_epoch, ack_delivery_id: update.params.ack_delivery_id,
    dirty_keys: [], global_dirty: false,
  } })
}

beforeEach(() => {
  vi.useFakeTimers()
  Socket.instances = []
  localStorage.clear()
  vi.stubGlobal('WebSocket', Socket)
})
afterEach(() => {
  for (const rpc of clients.splice(0)) rpc.disconnect()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('production connection integration boundaries', () => {
  it('reports the complete confirmed-failure recovery duration without connection secrets or message bodies', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const rpc = client()
    const diagnostics: Array<Record<string, unknown>> = []
    rpc.on('_transport', detail => diagnostics.push(detail as Record<string, unknown>))
    rpc.connect('ws://private-host.invalid/ws?private=PRIVATE_URL', 'PRIVATE_TOKEN')
    const first = Socket.instances[0]
    hello(first, { ...localOwner, authenticated: true })
    const call = rpc.call('diagnostic.noop', { text: 'PRIVATE_BODY' })
    const request = JSON.parse(first.sent[first.sent.length - 1])
    first.receive({ type: 'res', id: request.id, ok: true, payload: { text: 'PRIVATE_RESPONSE' } })
    await call
    expect(diagnostics.filter(item => item.phase === 'hello')[0]).not.toHaveProperty('recoveryMs')
    first.close()
    await vi.advanceTimersByTimeAsync(500)
    Socket.instances[1].close()
    await vi.advanceTimersByTimeAsync(1000)
    hello(Socket.instances[2], { ...localOwner, authenticated: true })
    expect(diagnostics.filter(item => item.phase === 'hello')[1]).toMatchObject({ recoveryMs: 1500 })
    rpc.connect('ws://another-private-host.invalid/ws', 'PRIVATE_TOKEN')
    hello(Socket.instances[3], { ...localOwner, authenticated: true })
    expect(diagnostics.filter(item => item.phase === 'hello')[2]).not.toHaveProperty('recoveryMs')
    const encoded = JSON.stringify(diagnostics)
    for (const secret of ['private-host', 'PRIVATE_URL', 'PRIVATE_TOKEN', 'PRIVATE_BODY', 'PRIVATE_RESPONSE']) {
      expect(encoded).not.toContain(secret)
    }
  })

  it('observes scheduler loop lag without creating per-tick logs or retiring a suspended socket', async () => {
    const rpc = client()
    const diagnostics: Array<Record<string, unknown>> = []
    rpc.on('_transport', detail => diagnostics.push(detail as Record<string, unknown>))
    rpc.connect('ws://127.0.0.1:18790/ws')
    hello(Socket.instances[0])
    const initialCount = diagnostics.length
    await vi.advanceTimersByTimeAsync(2000)
    expect(diagnostics).toHaveLength(initialCount)
    vi.setSystemTime(Date.now() + 8000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(diagnostics.find(item => item.phase === 'scheduler_lag')).toMatchObject({
      loopLagMs: 8000, maxLoopLagMs: 8000,
    })
    expect(Socket.instances).toHaveLength(1)
    expect(rpc.state).toBe('connected')
  })

  it('accepts the legitimate auth:none Desktop owner despite authenticated=false', () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws', 'desktop-owned-nonce', { authentication: 'owner' })
    hello(Socket.instances[0])
    expect(rpc.state).toBe('connected')
    expect(rpc.lifecycle).toBe('connected')
  })

  it('does not treat local owner proximity as validation of an explicit browser token', () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws', 'explicit-browser-token')
    hello(Socket.instances[0])
    expect(rpc.state).toBe('disconnected')
    expect(rpc.recoveryReason).toBe('authentication_mismatch')
  })

  it('does not downgrade a rejected Desktop nonce to a guest or a non-owner token', () => {
    for (const authenticated of [false, true]) {
      const rpc = client()
      rpc.connect('ws://127.0.0.1:18790/ws', 'old-desktop-nonce', { authentication: 'owner' })
      hello(Socket.instances[Socket.instances.length - 1], {
        ...localOwner, isOwner: false, authenticated, authState: authenticated ? 'authenticated' : 'guest',
      })
      expect(rpc.state).toBe('disconnected')
      expect(rpc.recoveryReason).toBe('authentication_mismatch')
    }
  })

  it('allows an explicit same-intent retry after authentication is repaired, but observations stay blocked', () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws', 'fixed-token')
    hello(Socket.instances[0], { ...localOwner, isOwner: false, authState: 'guest' })
    expect(rpc.lifecycle).toBe('blocked')
    rpc.ensureConnected()
    rpc.notifyResume()
    expect(Socket.instances).toHaveLength(1)
    rpc.connect('ws://127.0.0.1:18790/ws', 'fixed-token')
    expect(Socket.instances).toHaveLength(2)
    hello(Socket.instances[1], { ...localOwner, authenticated: true })
    expect(rpc.state).toBe('connected')
  })

  it('installs flow capability synchronously after connect and before the first challenge', async () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws', 'desktop-owned-nonce', { authentication: 'owner' })
    const socket = Socket.instances[0]
    // main.ts deliberately initializes the store first, then builds Adapters
    // in the same JavaScript turn, before WebSocket event callbacks can run.
    const { source, transport: first } = transports(rpc)
    createPrivateGatewayTransports(source)
    const consumed = vi.fn(async () => 'applied' as const)
    first.events.subscribeConsumed!('session.event.text_delta', consumed)
    hello(socket, localOwner, {
      transport_probe_nonce: true,
      transport_flow: { delivery_epoch: 'epoch-1', window_frames: 128, window_bytes: 4194304 },
    })
    const handshake = JSON.parse(socket.sent[0])
    expect(handshake.params.caps).toContain('transport.flow.v1')
    expect(handshake.params.caps).toContain('transport.probe.v1')
    socket.receive({
      type: 'event', event: 'session.event.text_delta', payload: { session_key: 'alpha', text_delta: 'x' },
      meta: { flow: { delivery_epoch: 'epoch-1', delivery_id: 1 } }, seq: 1,
    })
    await vi.advanceTimersByTimeAsync(50)
    expect(consumed).toHaveBeenCalledOnce()
    const updates = socket.sent.map(frame => JSON.parse(frame)).filter(frame => frame.method === 'transport.flow.update')
    expect(updates).toHaveLength(1)
    expect(updates[0].params).toEqual({ delivery_epoch: 'epoch-1', ack_delivery_id: 1 })
    socket.receive({ type: 'res', id: updates[0].id, ok: true, payload: {
      delivery_epoch: 'epoch-1', ack_delivery_id: 1, dirty_keys: [], global_dirty: false,
    } })
    await vi.advanceTimersByTimeAsync(0)
  })

  it.each(['timeout', 'abort'])('discards a valid orphan snapshot response after request-local %s', async reason => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[0]
    const { transport } = transports(rpc)
    hello(socket, localOwner, flowPolicy)
    const controller = new AbortController()
    const failure = transport.rpc.request('sessions.messages.snapshot.read', { key: 'alpha', sync_revision: 'sync-1' }, {
      timeoutMs: 10, signal: controller.signal,
    }).catch(error => error)
    const request = JSON.parse(socket.sent[socket.sent.length - 1])
    if (reason === 'abort') controller.abort()
    await vi.advanceTimersByTimeAsync(10)
    expect(await failure).toBeInstanceOf(Error)
    socket.receive({ type: 'res', id: request.id, ok: true, payload: snapshotResult() })
    await vi.advanceTimersByTimeAsync(0)
    const updates = flowUpdates(socket)
    expect(updates).toHaveLength(1)
    expect(updates[0].params).toEqual({ delivery_epoch: 'epoch-1', ack_delivery_id: 1, staged_delivery_ids: [1] })
    expect(updates[0].params.resume).toBeUndefined()
    confirmUpdate(socket)
    await vi.advanceTimersByTimeAsync(0)
    expect(rpc.state).toBe('connected')
  })

  it('does not forget orphan recovery ownership after an arbitrary tombstone TTL', async () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[0]
    socket.autoPong = true
    const { transport } = transports(rpc)
    hello(socket, localOwner, flowPolicy)
    const failure = transport.rpc.request('sessions.messages.snapshot.read', {}, { timeoutMs: 10 }).catch(error => error)
    const request = JSON.parse(socket.sent[socket.sent.length - 1])
    await vi.advanceTimersByTimeAsync(120000)
    expect(await failure).toBeInstanceOf(Error)
    expect(Socket.instances).toHaveLength(1)
    socket.receive({ type: 'res', id: request.id, ok: true, payload: snapshotResult() })
    await vi.advanceTimersByTimeAsync(0)
    expect(flowUpdates(socket)).toHaveLength(1)
    confirmUpdate(socket)
    await vi.advanceTimersByTimeAsync(0)
  })

  it('never guesses a recovery receipt from a generic or malformed orphan payload', async () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[0]
    transports(rpc)
    hello(socket, localOwner, flowPolicy)
    for (const payload of [
      { delivery: { delivery_epoch: 'epoch-1', delivery_id: 1 } },
      { ...snapshotResult(), segment_count: -1 },
      { ...snapshotResult(), extra: 'not in the snapshot contract' },
      { ...snapshotResult(), delivery: { delivery_epoch: 'old-epoch', delivery_id: 1 } },
    ]) socket.receive({ type: 'res', id: 'unknown', ok: true, payload })
    await vi.advanceTimersByTimeAsync(100)
    expect(flowUpdates(socket)).toHaveLength(0)
  })

  it('retains a late orphan discard behind an earlier credit update awaiting its reply', async () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[0]
    const { transport } = transports(rpc)
    hello(socket, localOwner, flowPolicy)
    const firstConfirmation = transport.rpc.acknowledgeDelivery!({ delivery_epoch: 'epoch-1', delivery_id: 1 })
    const firstUpdate = flowUpdates(socket)[0]
    // The server has applied the first update and sent another recovery piece,
    // but the first update's response is delayed behind that abandoned read.
    socket.receive({ type: 'res', id: 'abandoned-read', ok: true, payload: {
      ...snapshotResult(), delivery: { delivery_epoch: 'epoch-1', delivery_id: 2 },
    } })
    await vi.advanceTimersByTimeAsync(0)
    expect(flowUpdates(socket)).toHaveLength(1)
    socket.receive({ type: 'res', id: firstUpdate.id, ok: true, payload: {
      delivery_epoch: 'epoch-1', ack_delivery_id: 1, dirty_keys: [], global_dirty: false,
    } })
    await firstConfirmation
    await vi.advanceTimersByTimeAsync(50)
    expect(flowUpdates(socket)).toHaveLength(2)
    expect(flowUpdates(socket)[1].params).toEqual({
      delivery_epoch: 'epoch-1', ack_delivery_id: 2, staged_delivery_ids: [2],
    })
    confirmUpdate(socket)
    await vi.advanceTimersByTimeAsync(0)
  })

  it('never treats an owned response as an orphan or credits a replaced socket', async () => {
    const rpc = client()
    rpc.connect('ws://127.0.0.1:18790/ws')
    const socket = Socket.instances[0]
    const { transport } = transports(rpc)
    hello(socket, localOwner, flowPolicy)
    const result = transport.rpc.request('sessions.messages.snapshot.read')
    const request = JSON.parse(socket.sent[socket.sent.length - 1])
    socket.receive({ type: 'res', id: request.id, ok: true, payload: snapshotResult() })
    await result
    expect(flowUpdates(socket)).toHaveLength(0)
    rpc.connect('ws://127.0.0.1:18791/ws')
    const replacement = Socket.instances[1]
    hello(replacement, localOwner, {
      transport_flow: { delivery_epoch: 'epoch-2', window_frames: 128, window_bytes: 4194304 },
    })
    socket.receive({ type: 'res', id: 'late', ok: true, payload: snapshotResult() })
    await vi.advanceTimersByTimeAsync(100)
    expect(flowUpdates(replacement)).toHaveLength(0)
    expect(rpc.state).toBe('connected')
  })
})
