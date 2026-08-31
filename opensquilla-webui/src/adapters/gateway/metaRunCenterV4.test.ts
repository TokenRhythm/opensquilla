import { describe, expect, it, vi } from 'vitest'

import { createV4MetaRunCenter } from './metaRunCenterV4'

function source(response: unknown = {}) {
  const requests: Array<{ method: string; params?: Record<string, unknown> }> = []
  const transport = {
    requests,
    ready: vi.fn(async () => {}),
    supports: vi.fn(() => true),
    markUnsupported: vi.fn(),
    request: vi.fn(async (method: string, params?: Record<string, unknown>) => {
      requests.push({ method, params })
      return response
    }) as unknown as <T = unknown>(method: string, params?: Record<string, unknown>, options?: unknown) => Promise<T>,
  }
  const listeners = new Map<string, (payload: unknown, meta?: unknown) => void>()
  const events = {
    subscribe: vi.fn((name: string, handler: (payload: unknown, meta?: unknown) => void) => {
      listeners.set(name, handler)
      return { close: vi.fn() }
    }),
  }
  return { transport, events, listeners }
}

describe('createV4MetaRunCenter', () => {
  it('keeps launch and setup wire names behind the Meta domain seam', async () => {
    const fixture = source({ ok: true, sessionKey: 'agent:main:test', clientRequestId: 'req-1' })
    const center = createV4MetaRunCenter(fixture.transport, fixture.events)
    await expect(center.launch({
      name: 'meta-paper-write',
      sessionKey: 'agent:main:test',
      clientRequestId: 'req-1',
      launchText: '/meta meta-paper-write',
    })).resolves.toMatchObject({ ok: true, sessionKey: 'agent:main:test' })
    await center.setupStatus({ jobId: 'job-1', sessionKey: 'agent:main:test' })
    expect(fixture.transport.requests.map(request => request.method)).toEqual([
      'meta.run',
      'meta.setup.status',
    ])
  })

  it('projects durable drafts and keeps the old aliases accepted', async () => {
    const fixture = source({
      durable: true,
      drafts: [{
        session_key: 'agent:main:test',
        client_request_id: 'req-1',
        meta_skill_name: 'meta-paper-write',
        launch_text: '/meta meta-paper-write',
        created_at: 1,
        expires_at: 2,
        session_exists: false,
      }],
    })
    const center = createV4MetaRunCenter(fixture.transport, fixture.events)
    await expect(center.listDrafts({ agentId: 'main' })).resolves.toEqual({
      durable: true,
      drafts: [{
        sessionKey: 'agent:main:test',
        clientRequestId: 'req-1',
        name: 'meta-paper-write',
        launchText: '/meta meta-paper-write',
        createdAt: 1,
        expiresAt: 2,
        sessionExists: false,
      }],
    })
  })

  it('decodes canonical and bare event aliases into one subscription', () => {
    const fixture = source()
    const center = createV4MetaRunCenter(fixture.transport, fixture.events)
    const received: unknown[] = []
    const subscription = center.subscribe(event => received.push(event))
    fixture.listeners.get('meta_run_announced')?.({
      session_key: 'agent:main:test',
      stream_seq: 4,
      run_id: 'run-1',
      schema_version: 1,
    })
    expect(received).toEqual([expect.objectContaining({
      kind: 'run-announced',
      sessionKey: 'agent:main:test',
      streamSeq: 4,
    })])
    subscription.close()
  })
})
