import { describe, expect, it, vi } from 'vitest'

import { createV4PendingInputQueue } from './pendingInputQueueV4'

describe('pending input queue v4 adapter', () => {
  it('projects domain operations to the four queue RPCs and hides wire names', async () => {
    const request = vi.fn(async (method: string) => {
      if (method.endsWith('.list')) return { items: [{ pendingInputId: 'p1', clientRequestId: 'r1', clientMessageId: 'm1' }] }
      if (method.endsWith('.enqueue')) return { requestFingerprint: 'fp', revision: 2 }
      if (method.endsWith('.reorder')) return { items: [] }
      return {}
    })
    const adapter = createV4PendingInputQueue({
      request: request as unknown as <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>,
      supports: () => true,
    })

    expect(adapter.supportsQueue()).toBe(true)
    expect(adapter.supportsReorder()).toBe(true)
    await adapter.enqueue({
      key: 's', pendingInputId: 'p1', message: 'hello', attachments: [],
    })
    await expect(adapter.list('s')).resolves.toEqual([{ pendingInputId: 'p1', clientRequestId: 'r1', clientMessageId: 'm1' }])
    await adapter.cancel({ key: 's', pendingInputId: 'p1' })
    await adapter.reorder({ key: 's', items: [] })
    expect(request.mock.calls.map(([method]) => method)).toEqual([
      'sessions.pending_inputs.enqueue',
      'sessions.pending_inputs.list',
      'sessions.pending_inputs.cancel',
      'sessions.pending_inputs.reorder',
    ])
  })

  it('keeps connection waiting behind the adapter seam', async () => {
    const ready = vi.fn(async () => undefined)
    const adapter = createV4PendingInputQueue({
      request: vi.fn(async () => ({})) as unknown as <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>,
      supports: () => false,
      ready,
    })
    await adapter.waitForConnection?.({ timeoutMs: 1000 })
    expect(ready).toHaveBeenCalledWith({ timeoutMs: 1000 })
  })
})
