import { describe, expect, it, vi } from 'vitest'
import { SessionReadContractError } from '@/modules/sessionReadLifecycle'
import { readV4SessionSnapshot } from './sessionSnapshotReadV4'

function harness(patch?: (segment: Record<string, unknown>, index: number) => void) {
  const value = {
    key: 'alpha', task_id: 'task-1', stream_generation: 'stream-1', current_stream_seq: 42,
    events: [{ event: 'session.event.text_delta', payload: { task_id: 'task-1', text_delta: '你'.repeat(100_000) } }],
  }
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  const chunkSize = 192 * 1024
  const acknowledgeDelivery = vi.fn()
  const resumeFlow = vi.fn(async () => {})
  let generation = 7
  const request = vi.fn(async (_method: string, params: Record<string, unknown> = {}) => {
    const index = Number(params.segment_index ?? 0)
    if (index > 0) expect(acknowledgeDelivery).toHaveBeenCalledTimes(index)
    const segment: Record<string, unknown> = {
      key: 'alpha', sync_revision: params.sync_revision, snapshot_id: 'snapshot-1',
      segment_index: index, segment_count: Math.ceil(bytes.length / chunkSize), byte_length: bytes.length,
      encoding: 'base64-json-utf8', stream_generation: 'stream-1', current_stream_seq: 42,
      session_id: 'session-1', session_epoch: 3, task_id: 'task-1',
      data: Buffer.from(bytes.subarray(index * chunkSize, (index + 1) * chunkSize)).toString('base64'),
      delivery: { delivery_epoch: 'delivery-1', delivery_id: index + 1 },
    }
    patch?.(segment, index)
    return segment
  })
  const rpc = {
    request: <T = unknown>(method: string, params?: Record<string, unknown>) => request(method, params) as Promise<T>,
    acknowledgeDelivery, resumeFlow, get generation() { return generation },
  }
  return { rpc, request, value, acknowledgeDelivery, resumeFlow, replaceConnection: () => { generation++ } }
}

describe('bounded session snapshot staging', () => {
  it('awaits discard credit for a validated same-connection result racing abort, without installing it', async () => {
    const controller = new AbortController()
    const h = harness(() => controller.abort())
    let release!: () => void
    h.acknowledgeDelivery.mockImplementationOnce(() => new Promise<void>(resolve => { release = resolve }))
    const pending = readV4SessionSnapshot(h.rpc, 'alpha', controller.signal, 7)
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.waitFor(() => expect(h.acknowledgeDelivery).toHaveBeenCalledOnce())
    expect(h.request).toHaveBeenCalledTimes(1)
    release()
    await rejected
    expect(h.request).toHaveBeenCalledTimes(1)
    expect(h.resumeFlow).not.toHaveBeenCalled()
  })

  it('never acknowledges a late segment on the replacement connection', async () => {
    const h = harness(() => h.replaceConnection())
    await expect(readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7)).rejects.toMatchObject({ name: 'AbortError' })
    expect(h.acknowledgeDelivery).not.toHaveBeenCalled()
    expect(h.resumeFlow).not.toHaveBeenCalled()
  })

  it('waits for staged-credit confirmation before admitting the next segment', async () => {
    const h = harness()
    let release!: () => void
    h.acknowledgeDelivery.mockImplementationOnce(() => new Promise<void>(resolve => { release = resolve }))
    const pending = readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7)
    await vi.waitFor(() => expect(h.acknowledgeDelivery).toHaveBeenCalledOnce())
    expect(h.request).toHaveBeenCalledTimes(1)
    release()
    await pending
    expect(h.request).toHaveBeenCalledTimes(2)
  })

  it('ACKs owned segments before requesting more, but resumes only after explicit installation', async () => {
    const h = harness()
    const staged = await readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7)
    expect(staged.value).toEqual(h.value)
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(h.resumeFlow).not.toHaveBeenCalled()
    await staged.confirmInstalled()
    await staged.confirmInstalled()
    expect(h.resumeFlow).toHaveBeenCalledOnce()
    expect(h.resumeFlow).toHaveBeenCalledWith(expect.objectContaining({ key: 'alpha', stream_seq: 42 }))
  })

  it.each(['session_id', 'session_epoch', 'stream_generation', 'snapshot_id', 'sync_revision'])(
    'rejects changed %s without installing partial state', async field => {
      const h = harness((segment, index) => { if (index === 1) segment[field] = field === 'session_epoch' ? 4 : 'changed' })
      await expect(readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7))
        .rejects.toBeInstanceOf(SessionReadContractError)
      expect(h.resumeFlow).not.toHaveBeenCalled()
      expect(h.acknowledgeDelivery).toHaveBeenCalledTimes(1)
    },
  )

  it('rejects oversized metadata before accepting any bytes', async () => {
    const h = harness(segment => { segment.byte_length = 26 * 1024 * 1024 })
    await expect(readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7)).rejects.toBeInstanceOf(SessionReadContractError)
    expect(h.acknowledgeDelivery).not.toHaveBeenCalled()
  })

  it('does not release a replacement connection from a late install callback', async () => {
    const h = harness()
    const staged = await readV4SessionSnapshot(h.rpc, 'alpha', new AbortController().signal, 7)
    h.replaceConnection()
    await expect(staged.confirmInstalled()).rejects.toMatchObject({ name: 'AbortError' })
    expect(h.resumeFlow).not.toHaveBeenCalled()
  })
})
