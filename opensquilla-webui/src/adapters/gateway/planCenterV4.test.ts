import { describe, expect, it, vi } from 'vitest'
import { createV4PlanCenter } from './planCenterV4'

describe('createV4PlanCenter', () => {
  it('keeps v4 method names inside the adapter', async () => {
    const request = vi.fn(async () => ({ sessionKey: 'agent:main:webchat:one', accepted: true }))
    const center = createV4PlanCenter({ request }, { subscribe: vi.fn(() => ({ close: vi.fn() })) })
    await expect(center.setMode('agent:main:webchat:one', 'plan', 2)).resolves.toMatchObject({ accepted: true })
    expect(request).toHaveBeenCalledWith('plans.setMode', {
      sessionKey: 'agent:main:webchat:one', mode: 'plan', expectedRevision: 2,
    }, undefined)
  })

  it('normalizes legacy event aliases into domain events', () => {
    const listeners = new Map<string, (payload: unknown) => void>()
    const events = { subscribe: vi.fn((name: string, handler: (payload: unknown) => void) => {
      listeners.set(name, handler)
      return { close: vi.fn() }
    }) }
    const center = createV4PlanCenter({ request: vi.fn() }, events)
    const received: unknown[] = []
    center.subscribe(event => received.push(event))
    listeners.get('plan_revision')?.({ session_key: 'agent:main:webchat:one', plan_revision: { revisionId: 'r1' } })
    expect(received).toEqual([expect.objectContaining({ kind: 'revision', sessionKey: 'agent:main:webchat:one' })])
  })
})
