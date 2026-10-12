// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcClient } from '@/lib/rpc'
import { createV4CronScheduler } from './cronSchedulerV4'

class Socket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: Socket[] = []
  readyState = Socket.OPEN
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null
  constructor(readonly url: string) { Socket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close(code = 1000, reason = '') {
    this.readyState = Socket.CLOSED
    this.onclose?.({ code, reason, wasClean: true } as CloseEvent)
  }
  receive(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent) }
  requests() { return this.sent.map(data => JSON.parse(data)).filter(frame => frame.type === 'req') }
}
const clients: RpcClient[] = []
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }
function connected() {
  const client = new RpcClient(); clients.push(client); client.connect('ws://cron.test')
  const socket = Socket.instances[Socket.instances.length - 1]!
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    type: 'hello-ok', protocol: 3, server: { version: 'test', conn_id: 'cron-test' },
    features: { methods: ['cron.list', 'cron.runs', 'cron.run'], events: [] },
    snapshot: {}, policy: { tick_interval_ms: 30_000, transport_probe_nonce: true }, auth: null,
  })
  const scheduler = createV4CronScheduler({
    get generation() { return client.connectionGeneration },
    request: (method, params, options) => client.call(method, params, options) as never,
    ready: options => client.ready(options?.timeoutMs, options?.signal, options),
  }, { subscribe: (event, listener) => ({ close: client.on(event, listener) }) })
  return { client, socket, scheduler }
}
beforeEach(() => { vi.useFakeTimers(); Socket.instances = []; vi.stubGlobal('WebSocket', Socket) })
afterEach(() => { clients.splice(0).forEach(client => client.disconnect()); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('Cron reads through the actual RpcClient', () => {
  it('waits for a checked connection to recover while never replaying runNow', async () => {
    const { client, socket, scheduler } = connected()
    client.notifyResume('desktop-resume')
    expect(client.state).toBe('connected')
    expect(client.phase).toBe('checking')
    const jobs = scheduler.listJobs(), runs = scheduler.listRuns('A')
    await expect(scheduler.runNow('A')).rejects.toThrow('being checked')
    await flush()
    expect(socket.requests().filter(frame => frame.method.startsWith('cron.'))).toEqual([])
    await vi.advanceTimersByTimeAsync(100)
    const ping = socket.sent.map(data => JSON.parse(data)).find(frame => frame.type === 'ping')
    socket.receive({ type: 'pong', nonce: ping.nonce }); await flush()
    const reads = socket.requests().filter(frame => frame.method.startsWith('cron.'))
    expect(reads.map(frame => frame.method)).toEqual(['cron.list', 'cron.runs'])
    for (const read of reads) socket.receive({ type: 'res', id: read.id, ok: true, payload: [] })
    await expect(jobs).resolves.toEqual([]); await expect(runs).resolves.toEqual([])
    expect(client.phase).toBe('healthy')
    expect(socket.requests().some(frame => frame.method === 'cron.run')).toBe(false)
  })

  it('cancels a queued read before the health probe completes', async () => {
    const { client, socket, scheduler } = connected()
    client.notifyResume('desktop-resume')
    const controller = new AbortController()
    const read = scheduler.listJobs({ signal: controller.signal })
    await flush(); controller.abort()
    await expect(read).rejects.toMatchObject({ code: 'RPC_ABORTED' })
    await vi.advanceTimersByTimeAsync(100)
    const ping = socket.sent.map(data => JSON.parse(data)).find(frame => frame.type === 'ping')
    socket.receive({ type: 'pong', nonce: ping.nonce }); await flush()
    expect(socket.requests().some(frame => frame.method === 'cron.list')).toBe(false)
  })

  it('bounds an unanswered read without recycling the shared connection', async () => {
    const { client, socket, scheduler } = connected()
    const read = scheduler.listJobs()
    const failed = expect(read).rejects.toMatchObject({ code: 'RPC_TIMEOUT' })
    await vi.advanceTimersByTimeAsync(10_000); await failed
    expect(client.state).toBe('connected')
    expect(socket.readyState).toBe(Socket.OPEN)
    expect(Socket.instances).toHaveLength(1)
  })
})
