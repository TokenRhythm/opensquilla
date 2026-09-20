import { afterEach, describe, expect, it, vi } from 'vitest'
import { createV4SessionSnapshotTransfer } from './sessionSnapshotReadV4'
import type { TransportCallOptions } from './transportTypes'

const READ = 'sessions.messages.snapshot.read'
const RESUME = 'sessions.messages.resume'
const RELEASE = 'sessions.messages.snapshot.release'
const PIECE = 192 * 1024

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
function harness(size = 8) {
  const bytes = new TextEncoder().encode(JSON.stringify({
    key: 'alpha', task_id: 'task-1', stream_generation: 'stream-1', current_stream_seq: 10,
    events: [{ event: 'session.event.text_delta', payload: { text: 'x'.repeat(size) } }],
  }))
  const controller = new AbortController()
  let invalidation = '0'
  const requests: Array<{ method: string; params: Record<string, unknown> }> = []
  const respond = (params: Record<string, unknown>) => {
    const index = Number(params.segment_index ?? 0)
    return {
      key: 'alpha', sync_revision: params.sync_revision, snapshot_id: 'snapshot-1',
      segment_index: index, segment_count: Math.ceil(bytes.length / PIECE), byte_length: bytes.length,
      encoding: 'base64-json-utf8', data: Buffer.from(bytes.subarray(index * PIECE, (index + 1) * PIECE)).toString('base64'),
      stream_generation: 'stream-1', current_stream_seq: 10, session_id: 'session-1', session_epoch: 1,
      task_id: 'task-1', delivery: { delivery_epoch: 'epoch-1', delivery_id: index + 1 },
    }
  }
  const read = vi.fn(async (params: Record<string, unknown>) => respond(params))
  const resume = vi.fn(async (params: Record<string, unknown>) => ({
    ...params, session_id: 'session-1', session_epoch: 1, replay_to_seq: 12,
  }))
  const consume = vi.fn(async () => {})
  const credit = vi.fn(async () => {})
  const request = vi.fn(async (method: string, params: Record<string, unknown> = {}, options?: TransportCallOptions) => {
    requests.push({ method, params })
    options?.onSent?.(1)
    if (method === READ) return read(params)
    if (method === RESUME) return resume(params)
    if (method === RELEASE) return { ...params, retired: true }
    throw new Error('Unexpected method')
  })
  const rpc = {
    generation: 1, supports: () => true,
    request: <T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions) => request(method, params, options) as Promise<T>,
    acknowledgeDelivery: credit, waitForConsumption: consume, recoveryVersion: () => invalidation,
  }
  const newTransfer = () => createV4SessionSnapshotTransfer(rpc, 'alpha', controller.signal, 1)
  const transfer = newTransfer()
  return { transfer, newTransfer, read, resume, respond, consume, credit, requests, controller, invalidate: () => { invalidation = '1' } }
}

afterEach(() => vi.useRealTimers())
describe('persistent recovery transfer', () => {
  it.each(['SNAPSHOT_STALE', 'SNAPSHOT_EXPIRED'])(
    'retires a first read rejected with %s and admits a fresh sync revision', async code => {
      const h = harness()
      h.read.mockRejectedValueOnce(Object.assign(new Error(code), { code }))
      await expect(h.transfer.read()).rejects.toThrow(code)
      expect(h.transfer.retired).toBe(true)
      const next = h.newTransfer()
      await (await next.read()).confirmInstalled()
      const reads = h.requests.filter(item => item.method === READ)
      expect(reads).toHaveLength(2)
      expect(reads[1].params.sync_revision).not.toBe(reads[0].params.sync_revision)
      expect(h.requests.find(item => item.method === RELEASE)?.params.snapshot_id).toBeUndefined()
      next.release()
    },
  )

  it.each(['SNAPSHOT_STALE', 'SNAPSHOT_EXPIRED'])(
    'replaces the frozen base when the resume proof rejects with %s', async code => {
      const h = harness()
      const staged = await h.transfer.read()
      h.resume.mockRejectedValueOnce(Object.assign(new Error(code), { code }))
      await expect(staged.confirmInstalled()).rejects.toThrow(code)
      expect(h.transfer.retired).toBe(true)
      const next = h.newTransfer()
      await (await next.read()).confirmInstalled()
      const resumes = h.requests.filter(item => item.method === RESUME)
      expect(resumes).toHaveLength(2)
      expect(resumes[1].params.sync_revision).not.toBe(resumes[0].params.sync_revision)
      expect(h.read).toHaveBeenCalledTimes(2)
      next.release()
    },
  )

  it.each(['SNAPSHOT_BUSY', 'RPC_TIMEOUT'])(
    'keeps the same staged owner when resume fails with %s', async code => {
      const h = harness()
      const staged = await h.transfer.read()
      h.resume.mockRejectedValueOnce(Object.assign(new Error(code), { code }))
      await expect(staged.confirmInstalled()).rejects.toThrow(code)
      expect(h.transfer.retired).toBe(false)
      await staged.confirmInstalled()
      const resumes = h.requests.filter(item => item.method === RESUME)
      expect(resumes).toHaveLength(2)
      expect(resumes[1].params).toEqual(resumes[0].params)
      expect(h.read).toHaveBeenCalledTimes(1)
      h.transfer.release()
    },
  )

  it('retains validated bytes, sync revision and next index after a segment timeout', async () => {
    const h = harness(PIECE)
    h.read.mockImplementationOnce(async params => h.respond(params))
      .mockRejectedValueOnce(Object.assign(new Error('piece timeout'), { code: 'RPC_TIMEOUT' }))
    await expect(h.transfer.read()).rejects.toThrow('piece timeout')
    const staged = await h.transfer.read()
    const reads = h.requests.filter(item => item.method === READ)
    expect(reads.map(item => item.params.segment_index ?? 0)).toEqual([0, 1, 1])
    expect(new Set(reads.map(item => item.params.sync_revision)).size).toBe(1)
    expect(h.credit).toHaveBeenCalledTimes(2)
    await staged.confirmInstalled()
    expect(h.requests.filter(item => item.method === RESUME)).toHaveLength(1)
    h.transfer.release()
  })

  it('keeps a slow progressing 45 second transfer and uses 15 second segment budgets', async () => {
    vi.useFakeTimers()
    const h = harness(7 * PIECE)
    h.read.mockImplementation(params => new Promise(resolve => setTimeout(() => resolve(h.respond(params)),
      Number(params.segment_index ?? 0) === 0 ? 3_000 : 6_000)))
    let done = false
    const pending = h.transfer.read().then(value => { done = true; return value })
    await vi.advanceTimersByTimeAsync(15_000)
    expect(done).toBe(false)
    expect(h.transfer.retired).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    const staged = await pending
    expect(done).toBe(true)
    expect(h.read).toHaveBeenCalledTimes(8)
    await staged.confirmInstalled()
    h.transfer.release()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not reset the absolute budget across repeated timed-out operations', async () => {
    vi.useFakeTimers()
    const h = harness()
    h.read.mockImplementation(() => new Promise((_resolve, reject) => setTimeout(() => reject(
      Object.assign(new Error('piece timeout'), { code: 'RPC_TIMEOUT' }),
    ), 15_000)))
    for (let attempt = 0; attempt < 8; attempt++) {
      const pending = h.transfer.read().catch(error => error)
      await vi.advanceTimersByTimeAsync(15_000)
      const error = await pending
      if (attempt === 7) expect(error).toMatchObject({ kind: 'budget-exhausted', retryable: false })
    }
    expect(h.transfer.retired).toBe(true)
    await expect(h.transfer.read()).rejects.toMatchObject({ kind: 'budget-exhausted' })
    expect(h.requests.filter(item => item.method === RELEASE)).toHaveLength(1)
    expect(h.requests.find(item => item.method === RELEASE)?.params.snapshot_id).toBeUndefined()
    expect(h.read).toHaveBeenCalledTimes(8)
  })

  it('requires the tail consumer and rejects invalidation after the network proof', async () => {
    const h = harness()
    const consumer = deferred<void>()
    h.consume.mockImplementation(() => consumer.promise)
    const staged = await h.transfer.read()
    let confirmed = false
    const confirmation = staged.confirmInstalled().then(() => { confirmed = true }, error => error)
    await Promise.resolve()
    expect(confirmed).toBe(false)
    h.invalidate()
    consumer.resolve()
    await expect(confirmation).resolves.toMatchObject({ kind: 'busy' })
    expect(confirmed).toBe(false)
    expect(h.transfer.retired).toBe(true)
  })

  it('releases an abandoned transfer between settled RPCs, without waiting for a new read', async () => {
    const h = harness()
    await h.transfer.read()
    h.controller.abort()
    await Promise.resolve()
    expect(h.transfer.retired).toBe(true)
    expect(h.requests.filter(item => item.method === RELEASE)).toEqual([expect.objectContaining({
      params: expect.objectContaining({ key: 'alpha', snapshot_id: 'snapshot-1' }),
    })])
    expect(h.requests.some(item => item.method === RESUME)).toBe(false)
  })
})
