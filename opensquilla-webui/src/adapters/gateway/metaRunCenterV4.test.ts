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

  it('decodes event aliases and projects Meta payloads into domain casing', () => {
    const fixture = source()
    const center = createV4MetaRunCenter(fixture.transport, fixture.events)
    const received: unknown[] = []
    const subscription = center.subscribe(event => received.push(event))
    fixture.listeners.get('meta_run_announced')?.({
      session_key: 'agent:main:test',
      epoch: 3,
      stream_seq: 4,
      stream_generation: 'generation-2',
      run_id: 'run-1',
      meta_skill_name: 'meta-paper-write',
      user_language: 'en',
      steps: [{ id: 'draft', depends_on: ['prepare'] }],
      schema_version: 1,
    })
    expect(received).toEqual([expect.objectContaining({
      kind: 'run-announced',
      sessionKey: 'agent:main:test',
      sessionEpoch: 3,
      streamSeq: 4,
      streamGeneration: 'generation-2',
      payload: expect.objectContaining({
        runId: 'run-1',
        metaSkillName: 'meta-paper-write',
        userLanguage: 'en',
        steps: [{
          id: 'draft',
          label: undefined,
          kind: undefined,
          dependsOn: ['prepare'],
        }],
      }),
    })])

    fixture.listeners.get('session.event.meta_preflight')?.({
      run_id: 'run-2',
      meta_skill_name: 'meta-paper-write',
      interpreted_request: 'Draft a paper',
      missing_fields: ['audience'],
      request_template: {
        fields: [{ name: 'audience', multiline: false }],
      },
      can_skip: false,
      requires_confirmation: true,
    })
    fixture.listeners.get('meta_step_state')?.({
      run_id: 'run-2',
      step_id: 'draft',
      state: 'failed',
      status_text: 'Needs revision',
      substitute_for: null,
    })
    fixture.listeners.get('session.event.meta_run_completed')?.({
      run_id: 'run-2',
      outcome: 'failed',
      completed_steps: ['outline'],
      failed_steps: ['draft'],
      recovered_steps: [],
      skipped_steps: [],
    })

    expect(received.slice(1)).toEqual([
      expect.objectContaining({
        kind: 'preflight',
        payload: expect.objectContaining({
          runId: 'run-2',
          metaSkillName: 'meta-paper-write',
          interpretedRequest: 'Draft a paper',
          missingFields: ['audience'],
          canSkip: false,
          requiresConfirmation: true,
        }),
      }),
      expect.objectContaining({
        kind: 'step-state',
        payload: expect.objectContaining({
          runId: 'run-2',
          stepId: 'draft',
          statusText: 'Needs revision',
          substituteFor: null,
        }),
      }),
      expect.objectContaining({
        kind: 'run-completed',
        payload: expect.objectContaining({
          runId: 'run-2',
          completedSteps: ['outline'],
          failedSteps: ['draft'],
          recoveredSteps: [],
          skippedSteps: [],
        }),
      }),
    ])
    for (const event of received) {
      expect((event as { payload: object }).payload).not.toHaveProperty('run_id')
    }
    subscription.close()
  })
})
