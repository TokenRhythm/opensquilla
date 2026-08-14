import { describe, expect, it } from 'vitest'

import {
  normalizeCollaborationSnapshot,
  normalizePlanRevisionSnapshot,
  normalizePlanRunSnapshot,
  payloadBelongsToSession,
  planRevisionsFromToolSegments,
} from './plans'

describe('plan payload normalization', () => {
  it('normalizes the collaboration envelope without losing a fallback revision', () => {
    expect(normalizeCollaborationSnapshot({
      session_key: 'agent:main:webchat:one',
      collaboration: { mode: 'plan', revision: 4 },
    })).toEqual({ mode: 'plan', revision: 4 })
    expect(normalizeCollaborationSnapshot({
      collaborationMode: 'plan',
      collaborationRevision: 5,
    })).toEqual({ mode: 'plan', revision: 5 })

    expect(normalizeCollaborationSnapshot(
      { collaboration: { mode: 'unknown' } },
      { mode: 'default', revision: 7 },
    )).toEqual({ mode: 'default', revision: 7 })
  })

  it('accepts plan revision envelopes from events, RPC responses, and history parts', () => {
    const payload = {
      revision_id: 'revision-2',
      plan_id: 'plan-1',
      parent_revision_id: 'revision-1',
      generation: 2,
      title: 'Ship plan mode',
      markdown: 'A complete plan.',
      steps: [{ step_id: 'inspect', title: 'Inspect', details: 'Read the runtime.' }],
      created_at: 123,
    }

    expect(normalizePlanRevisionSnapshot({ plan_revision: payload })).toEqual({
      revisionId: 'revision-2',
      planId: 'plan-1',
      parentRevisionId: 'revision-1',
      generation: 2,
      title: 'Ship plan mode',
      markdown: 'A complete plan.',
      steps: [{ stepId: 'inspect', title: 'Inspect', details: 'Read the runtime.' }],
      current: false,
      createdAt: 123,
    })
    expect(normalizePlanRevisionSnapshot({ planRevision: payload })?.revisionId)
      .toBe('revision-2')

    expect(planRevisionsFromToolSegments([
      { type: 'text', text: 'not a plan' },
      { type: 'plan', snapshot: payload },
    ], 'revision-2')).toEqual([
      expect.objectContaining({ revisionId: 'revision-2', current: true }),
    ])
  })

  it('normalizes server-authoritative run state and rejects malformed snapshots', () => {
    expect(normalizePlanRunSnapshot({
      plan_run: {
        run_id: 'run-1',
        plan_revision_id: 'revision-2',
        status: 'running',
        current_step_id: 'build',
        state_revision: 8,
        driver_kind: 'goal',
        driver_id: 'goal-1',
        active_task_id: 'task-1',
        step_states: [
          { step_id: 'inspect', title: 'Inspect', status: 'completed' },
          { step_id: 'build', title: 'Build', status: 'in_progress' },
        ],
      },
    })).toEqual({
      runId: 'run-1',
      planRevisionId: 'revision-2',
      status: 'running',
      currentStepId: 'build',
      stateRevision: 8,
      driverKind: 'goal',
      driverId: 'goal-1',
      activeTaskId: 'task-1',
      steps: [
        { stepId: 'inspect', title: 'Inspect', status: 'completed' },
        { stepId: 'build', title: 'Build', status: 'in_progress' },
      ],
    })

    expect(normalizePlanRevisionSnapshot({ title: 'Missing ids' })).toBeNull()
    expect(normalizePlanRunSnapshot({
      runId: 'run-1',
      planRevisionId: 'revision-2',
      status: 'invented',
    })).toBeNull()
  })

  it('filters events to the active session while accepting payloads without a key', () => {
    expect(payloadBelongsToSession(
      { session_key: 'agent:main:webchat:one' },
      'agent:main:webchat:one',
    )).toBe(true)
    expect(payloadBelongsToSession(
      { sessionKey: 'agent:main:webchat:two' },
      'agent:main:webchat:one',
    )).toBe(false)
    expect(payloadBelongsToSession({ planRun: {} }, 'agent:main:webchat:one')).toBe(true)
  })
})
