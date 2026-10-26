import { afterEach, describe, expect, it, vi } from 'vitest'
import { openV2SessionRead, type SessionReadV2Transport } from './sessionReadV2'
import type { TransportCallOptions } from './transportTypes'

const OPEN = 'sessions.read.open.v2'
const STATE = 'sessions.read.state.v2'
const INSTALL = 'sessions.read.install.v2'
const CLOSE = 'sessions.read.close.v2'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function opened(key: string, suffix = '') {
  return {
    lease_id: `lease-${key}${suffix}`, recovery_id: `recovery-${key}${suffix}`, session_id: `id-${key}`,
    session_epoch: 3, connection_epoch: 'connection', subscription_epoch: `subscription-${key}`,
    state_revision: 1, base_stream_generation: 'stream-1', base_stream_seq: 9,
    state_manifest: { length: 0, sha256: 'a'.repeat(64), schema_version: 1, chunks: [] },
  }
}

interface Call { method: string; params: Record<string, unknown>; options: TransportCallOptions }
function harness(admit = true) {
  let generation = 1
  const calls: Call[] = []
  const replies = new Map<string, Array<Promise<unknown>>>()
  const request = vi.fn((method: string, params: Record<string, unknown> = {}, options: TransportCallOptions = {}) => {
    calls.push({ method, params, options })
    if (admit) options.onSent?.(generation)
    const held = replies.get(`${method}:${params.key}`)?.shift()
    let response: Promise<unknown>
    if (held) response = held
    else if (method === OPEN) response = Promise.resolve(opened(String(params.key)))
    else if (method === CLOSE) response = Promise.resolve({ lease_id: params.lease_id, closed: true, status: 'retired' })
    else if (method === STATE || method === INSTALL) response = Promise.resolve({
      lease_id: `lease-${params.key}`, recovery_id: `recovery-${params.key}`, status: 'installed',
      ...(method === STATE ? { session_id: `id-${params.key}`, session_epoch: 3, state_revision: 1 } : {}),
      progress: { status: 'installed', base_seq: 9, target_seq: 9, next_seq: 10, consumed_through_seq: 9 },
    })
    else response = Promise.resolve({ ok: true })
    return new Promise((resolve, reject) => {
      const cancel = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
      options.signal?.addEventListener('abort', cancel, { once: true })
      void response.then(resolve, reject).finally(() => options.signal?.removeEventListener('abort', cancel))
    })
  })
  const rpc: SessionReadV2Transport = {
    get generation() { return generation },
    request: (method, params, options) => request(method, params, options) as never,
  }
  return {
    rpc, calls, request,
    nextGeneration() { generation++ },
    hold(method: string, key: string) {
      const value = deferred<unknown>()
      const queue = replies.get(`${method}:${key}`) ?? []
      queue.push(value.promise)
      replies.set(`${method}:${key}`, queue)
      return value
    },
  }
}

async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve() }
const outcome = <T>(promise: Promise<T>) => promise.then(value => ({ value }), error => ({ error }))
afterEach(() => vi.useRealTimers())

describe('read-v2 wire ownership during view replacement', () => {
  it('drops unsent intermediate views while the old open drains and closes its exact lease', async () => {
    const h = harness()
    const a = new AbortController()
    const late = h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a', 1, a.signal))
    await flush()
    a.abort()
    const b = new AbortController()
    const middle = outcome(openV2SessionRead(h.rpc, 'b', 1, b.signal))
    await flush()
    b.abort()
    const latest = openV2SessionRead(h.rpc, 'c', 1, new AbortController().signal)
    await flush()
    expect(h.calls.map(call => [call.method, call.params.key])).toEqual([[OPEN, 'a']])
    expect(h.calls[0]!.options.signal!.aborted).toBe(false)
    late.resolve(opened('a'))
    const c = await latest
    await flush()
    expect(await first).toHaveProperty('error.name', 'AbortError')
    expect(await middle).toHaveProperty('error.name', 'AbortError')
    expect(h.calls.some(call => call.params.key === 'b')).toBe(false)
    expect(h.calls.filter(call => call.method === CLOSE).map(call => call.params.lease_id)).toEqual(['lease-a'])
    await c.close()
  })

  it('does not close one live conversation when a side chat opens on the same transport', async () => {
    const h = harness()
    const main = await openV2SessionRead(h.rpc, 'main')
    const side = await openV2SessionRead(h.rpc, 'side')
    expect(h.calls.filter(call => call.method === CLOSE)).toEqual([])
    await expect(main.state()).resolves.toMatchObject({ lease_id: 'lease-main' })
    await expect(side.state()).resolves.toMatchObject({ lease_id: 'lease-side' })
    await Promise.all([main.close(), side.close()])
  })

  it('waits for old same-key close before issuing a replacement open', async () => {
    const h = harness()
    const first = await openV2SessionRead(h.rpc, 'a')
    const close = h.hold(CLOSE, 'a')
    const retiring = first.close()
    const replacement = openV2SessionRead(h.rpc, 'a')
    await flush()
    expect(h.calls.filter(call => call.method === OPEN)).toHaveLength(1)
    close.resolve({ lease_id: 'lease-a', closed: true })
    await retiring
    const current = await replacement
    expect(h.calls.map(call => call.method)).toEqual([OPEN, CLOSE, OPEN])
    await current.close()
  })

  it('shares a same-key open across two live views and retires only the last consumer', async () => {
    const h = harness()
    const controller = new AbortController()
    const late = h.hold(OPEN, 'child')
    const mainOpen = outcome(openV2SessionRead(h.rpc, 'child', 1, controller.signal))
    const sideOpen = openV2SessionRead(h.rpc, 'child')
    await flush()
    controller.abort()
    expect(h.calls.filter(call => call.method === OPEN)).toHaveLength(1)
    expect(h.calls[0]!.options.signal!.aborted).toBe(false)
    late.resolve(opened('child'))
    const side = await sideOpen
    expect(await mainOpen).toHaveProperty('error.name', 'AbortError')
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(0)
    const main = await openV2SessionRead(h.rpc, 'child')
    await side.close()
    await side.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(0)
    await expect(main.state()).resolves.toMatchObject({ lease_id: 'lease-child' })
    await main.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(1)
  })

  it('lets a started install settle before closing without putting send or stop in the read coordinator', async () => {
    const h = harness()
    const controller = new AbortController()
    const lease = await openV2SessionRead(h.rpc, 'a', 1, controller.signal)
    const install = h.hold(INSTALL, 'a')
    const installed = outcome(lease.install({ baseApplied: true }))
    await flush()
    controller.abort()
    const closing = lease.close()
    await h.rpc.request('chat.send', { key: 'other' })
    await h.rpc.request('chat.abort', { key: 'other' })
    expect(h.calls.some(call => call.method === CLOSE)).toBe(false)
    expect(h.calls.find(call => call.method === INSTALL)!.options.signal!.aborted).toBe(false)
    install.resolve({ lease_id: 'lease-a', recovery_id: 'recovery-a', status: 'installed',
      progress: { status: 'installed', base_seq: 9, target_seq: 9, next_seq: 10, consumed_through_seq: 9 } })
    await closing
    expect(await installed).toHaveProperty('error.name', 'AbortError')
    expect(h.calls.map(call => call.method)).toEqual([OPEN, INSTALL, 'chat.send', 'chat.abort', CLOSE])
  })

  it('recovers a lost open response with exactly one FIFO probe and cleans before reopening', async () => {
    vi.useFakeTimers()
    const h = harness()
    h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a'))
    await flush()
    await vi.advanceTimersByTimeAsync(7_000)
    expect(await first).toHaveProperty('error.kind', 'timeout')
    await flush()
    expect(h.calls.map(call => call.method)).toEqual([OPEN, OPEN, CLOSE])
    expect(h.calls[0]!.options.signal!.aborted).toBe(true)
    const next = await openV2SessionRead(h.rpc, 'a')
    await next.close()
  })

  it('bounds two unanswered requests, fails clearly, and resumes after a late probe', async () => {
    vi.useFakeTimers()
    const h = harness()
    h.hold(OPEN, 'a')
    const probe = h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a'))
    await flush()
    await vi.advanceTimersByTimeAsync(7_000)
    const pending = outcome(openV2SessionRead(h.rpc, 'b'))
    await vi.advanceTimersByTimeAsync(7_000)
    expect(await first).toHaveProperty('error.kind', 'timeout')
    expect(await pending).toHaveProperty('error.kind', 'unresolved')
    for (let i = 0; i < 10; i++) {
      await expect(openV2SessionRead(h.rpc, `retry-${i}`)).rejects.toMatchObject({ kind: 'unresolved', retryable: false })
    }
    expect(h.calls).toHaveLength(2)
    probe.resolve(opened('a'))
    await flush()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(1)
    const next = await openV2SessionRead(h.rpc, 'b')
    await next.close()
  })

  it('does not treat a refused probe as a FIFO completion proof', async () => {
    vi.useFakeTimers()
    const h = harness()
    const original = h.hold(OPEN, 'a')
    const probe = h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a'))
    await flush()
    await vi.advanceTimersByTimeAsync(7_000)
    probe.reject(Object.assign(new Error('Connection request queue is full'), { code: 'UNAVAILABLE', accepted: false }))
    await vi.advanceTimersByTimeAsync(7_000)
    expect(await first).toHaveProperty('error.kind', 'timeout')
    await expect(openV2SessionRead(h.rpc, 'b')).rejects.toMatchObject({ kind: 'unresolved' })
    expect(h.calls[0]!.options.signal!.aborted).toBe(false)
    original.resolve(opened('a'))
    await flush()
    const next = await openV2SessionRead(h.rpc, 'b')
    await next.close()
  })

  it('treats exact repeated close READ_STALE as cleanup, never as a live install', async () => {
    vi.useFakeTimers()
    const h = harness()
    const lease = await openV2SessionRead(h.rpc, 'a')
    h.hold(CLOSE, 'a')
    const repeated = h.hold(CLOSE, 'a')
    const closing = lease.close()
    await flush()
    await vi.advanceTimersByTimeAsync(7_000)
    repeated.reject(Object.assign(new Error('No such lease'), { code: 'READ_STALE' }))
    await expect(closing).resolves.toBeNull()
    const next = await openV2SessionRead(h.rpc, 'a')
    await next.close()
  })

  it('retries a known close refusal once, retaining its identity for later cleanup', async () => {
    const h = harness()
    const first = await openV2SessionRead(h.rpc, 'a')
    const refused1 = h.hold(CLOSE, 'a')
    const refused2 = h.hold(CLOSE, 'a')
    const closing = outcome(first.close())
    await flush()
    refused1.reject(Object.assign(new Error('queue full'), { code: 'UNAVAILABLE', accepted: false }))
    await flush()
    refused2.reject(Object.assign(new Error('queue full'), { code: 'UNAVAILABLE', accepted: false }))
    expect(await closing).toHaveProperty('error.message', 'queue full')
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(2)
    const next = await openV2SessionRead(h.rpc, 'b')
    expect(h.calls.map(call => call.method)).toEqual([OPEN, CLOSE, CLOSE, CLOSE, OPEN])
    await next.close()
  })

  it.each(['abort', 'timeout'])('does not probe an unsent request after consumer %s', async reason => {
    vi.useFakeTimers()
    const h = harness(false)
    const controller = new AbortController()
    h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a', 1, controller.signal))
    await flush()
    if (reason === 'abort') controller.abort()
    else await vi.advanceTimersByTimeAsync(7_000)
    await flush()
    expect(await first).toHaveProperty(reason === 'abort' ? 'error.name' : 'error.kind', reason === 'abort' ? 'AbortError' : 'timeout')
    expect(h.calls[0]!.options.signal!.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(h.calls).toHaveLength(1)
  })

  it('shares concurrent same-key arrivals only after the old cleanup finishes', async () => {
    const h = harness()
    const old = await openV2SessionRead(h.rpc, 'a')
    const delayed = h.hold(CLOSE, 'a')
    const cleanup = old.close()
    const one = openV2SessionRead(h.rpc, 'a')
    const two = openV2SessionRead(h.rpc, 'a')
    await flush()
    expect(h.calls.filter(call => call.method === OPEN)).toHaveLength(1)
    delayed.resolve({ lease_id: 'lease-a', closed: true })
    await cleanup
    const [first, second] = await Promise.all([one, two])
    expect(h.calls.filter(call => call.method === OPEN)).toHaveLength(2)
    await first.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(1)
    await expect(second.state()).resolves.toMatchObject({ lease_id: 'lease-a' })
    await second.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(2)
  })

  it('releases a failed open consumer before a successful retry closes the lease', async () => {
    const h = harness()
    const refused = h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a'))
    await flush()
    refused.reject(Object.assign(new Error('busy'), { code: 'STORAGE_BUSY' }))
    expect(await first).toHaveProperty('error.code', 'STORAGE_BUSY')
    const next = await openV2SessionRead(h.rpc, 'a')
    await next.close()
    expect(h.calls.map(call => call.method)).toEqual([OPEN, OPEN, CLOSE])
  })

  it.each(['READ_STALE', 'REBASE_REQUIRED'])('retires a shared stale lease after %s so both views can obtain a new base', async code => {
    const h = harness()
    const first = await openV2SessionRead(h.rpc, 'a')
    const side = await openV2SessionRead(h.rpc, 'a')
    const stale = h.hold(STATE, 'a')
    const reading = outcome(first.state())
    await flush()
    stale.reject(Object.assign(new Error('lease replaced'), { code }))
    expect(await reading).toHaveProperty('error.kind', 'rebase-required')
    await expect(side.state()).rejects.toMatchObject({ kind: 'rebase-required' })
    const next = await openV2SessionRead(h.rpc, 'a')
    expect(h.calls.filter(call => call.method === OPEN)).toHaveLength(2)
    await side.close()
    await first.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(1)
    await expect(next.state()).resolves.toMatchObject({ lease_id: 'lease-a' })
    await next.close()
    expect(h.calls.filter(call => call.method === CLOSE)).toHaveLength(2)
  })

  it('discards only retired-generation work when the physical connection is replaced', async () => {
    vi.useFakeTimers()
    const h = harness()
    const late = h.hold(OPEN, 'a')
    h.hold(OPEN, 'a')
    const first = outcome(openV2SessionRead(h.rpc, 'a'))
    await flush()
    await vi.advanceTimersByTimeAsync(14_000)
    expect(await first).toHaveProperty('error.kind', 'timeout')
    h.nextGeneration()
    const next = await openV2SessionRead(h.rpc, 'b')
    late.resolve(opened('a'))
    await flush()
    expect(h.calls.filter(call => call.method === CLOSE && call.params.key === 'a')).toEqual([])
    await next.close()
  })
})
