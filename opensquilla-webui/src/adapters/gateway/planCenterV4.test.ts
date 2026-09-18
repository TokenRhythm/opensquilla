import { describe, expect, it, vi } from 'vitest'
import { createV4PlanCenter } from './planCenterV4'

describe('createV4PlanCenter', () => {
  it('keeps v4 method names inside the adapter', async () => {
    const request = vi.fn(async (_method: string, _params?: Record<string, unknown>, _options?: unknown) => ({ sessionKey: 'agent:main:webchat:one', accepted: true })) as unknown as (<T = unknown>(method: string, params?: Record<string, unknown>, options?: unknown) => Promise<T>)
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
    listeners.get('session.event.plan_presentation')?.({ sessionKey: 'agent:main:webchat:one', epoch: 7, planPresentations: [{ revisionId: 'r1', dismissed: true, stateRevision: 2 }] })
    expect(received[1]).toMatchObject({ kind: 'presentation', epoch: 7, planPresentations: [{ revisionId: 'r1', dismissed: true, stateRevision: 2 }] })
  })

  it('sends presentation CAS fields and detects support independently of execution methods', async () => {
    const request = vi.fn().mockResolvedValue({ planPresentations: [{ revisionId: 'r1', dismissed: true, stateRevision: 3 }] })
    const center = createV4PlanCenter({ request, supports: method => method === 'plans.setPresentation' }, { subscribe: vi.fn() })
    expect(center.available('presentation')).toBe(true)
    expect(center.available('mutations')).toBe(false)
    const input = { sessionKey: 'agent:main:webchat:one', revisionId: 'r1', dismissed: true, expectedEpoch: 2, expectedPresentationRevision: 2, clientRequestId: 'presentation-1' }
    await expect(center.setPresentation(input)).resolves.toMatchObject({ planPresentations: [{ stateRevision: 3 }] })
    expect(request).toHaveBeenCalledWith('plans.setPresentation', input, undefined)
    request.mockResolvedValueOnce({ accepted: true })
    await expect(center.setPresentation(input)).rejects.toThrow('invalid response')
  })
})
