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

  it('projects legacy aliases and attachment metadata into canonical domain rows', async () => {
    const request = vi.fn(async (method: string) => {
      if (method.endsWith('.list')) {
        return {
          items: [{
            pending_input_id: 'p-legacy',
            client_request_id: 'r-legacy',
            client_message_id: 'm-legacy',
            message: 'queued message',
            display_text: 'queued display',
            request_fingerprint: 'fp-legacy',
            prompt_annotation_ids: ['annotation-1'],
            intent: 'follow_up',
            confirmedPlainText: true,
            position: 3,
            revision: 7,
            attachments: [{
              name: 'notes.txt',
              type: 'text/plain',
              size: 12,
              file_uuid: 'must-not-cross-domain-seam',
            }],
          }],
        }
      }
      return {}
    })
    const adapter = createV4PendingInputQueue({
      request: request as unknown as <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>,
      supports: () => true,
    })

    await expect(adapter.list('s')).resolves.toEqual([{
      pendingInputId: 'p-legacy',
      clientRequestId: 'r-legacy',
      clientMessageId: 'm-legacy',
      message: 'queued message',
      displayText: 'queued display',
      requestFingerprint: 'fp-legacy',
      promptAnnotationIds: ['annotation-1'],
      intent: 'follow_up',
      confirmedPlainText: true,
      position: 3,
      revision: 7,
      attachments: [{ name: 'notes.txt', mime: 'text/plain', size: 12 }],
    }])
  })

  it('fails closed on malformed v4 responses in every operation', async () => {
    let response: unknown = { requestFingerprint: 'fp', revision: 1 }
    const request = vi.fn(async () => response)
    const adapter = createV4PendingInputQueue({
      request: request as unknown as <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>,
      supports: () => true,
    })
    const enqueue = {
      key: 's',
      pendingInputId: 'p1',
      message: 'hello',
      attachments: [],
    }

    await expect(adapter.enqueue(enqueue)).resolves.toMatchObject({
      requestFingerprint: 'fp',
      revision: 1,
    })

    response = { requestFingerprint: 'fp' }
    await expect(adapter.enqueue(enqueue)).rejects.toThrow('Invalid pending enqueue response')

    response = { items: 'not-an-array' }
    await expect(adapter.list('s')).rejects.toThrow('Invalid pending list response')

    response = { items: [{ pendingInputId: 'p1' }] }
    await expect(adapter.list('s')).rejects.toThrow('Invalid pending list response')

    response = { items: null }
    await expect(adapter.reorder({ key: 's', items: [] }))
      .rejects.toThrow('Invalid pending reorder response')

    response = { items: [{ pendingInputId: 'p1' }] }
    await expect(adapter.reorder({ key: 's', items: [] }))
      .rejects.toThrow('Invalid pending reorder response')

    response = null
    await expect(adapter.cancel({ key: 's', pendingInputId: 'p1' }))
      .rejects.toThrow('Invalid pending cancel response')

    response = undefined
    await expect(adapter.cancel({ key: 's', pendingInputId: 'p1' })).resolves.toBeUndefined()
  })
})
