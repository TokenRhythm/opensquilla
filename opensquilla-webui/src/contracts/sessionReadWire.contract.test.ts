import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

interface FixtureDocument {
  cases: Array<{
    id: string
    wire?: unknown
    assert?: Record<string, unknown>
  }>
}

const historyValidators = await import('./generated/v4/chatHistoryValidators.mjs') as {
  validateChatHistoryRequestFrame: ContractValidator
  validateChatHistoryResponseFrame: ContractValidator
}
const subscribeValidators = await import('./generated/v4/sessionsMessagesSubscribeValidators.mjs') as {
  validateSessionsMessagesSubscribeRequestFrame: ContractValidator
  validateSessionsMessagesSubscribeResponseFrame: ContractValidator
}
const hydrateValidators = await import('./generated/v4/sessionsMessagesHydrateValidators.mjs') as {
  validateSessionsMessagesHydrateRequestFrame: ContractValidator
  validateSessionsMessagesHydrateResponseFrame: ContractValidator
}
const snapshotValidators = await import('./generated/v4/sessionsMessagesSnapshotValidators.mjs') as {
  validateSessionsMessagesSnapshotRequestFrame: ContractValidator
  validateSessionsMessagesSnapshotResponseFrame: ContractValidator
}
const unsubscribeValidators = await import('./generated/v4/sessionsMessagesUnsubscribeValidators.mjs') as {
  validateSessionsMessagesUnsubscribeRequestFrame: ContractValidator
  validateSessionsMessagesUnsubscribeResponseFrame: ContractValidator
}
const previewValidators = await import('./generated/v4/sessionsPreviewValidators.mjs') as {
  validateSessionsPreviewRequestFrame: ContractValidator
  validateSessionsPreviewResponseFrame: ContractValidator
}

function historyFixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../tests/fixtures/gateway/chat_history/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

const emptyMetadata = {
  workspaceId: null,
  projectWorkspace: null,
  projectWorkspaceDeferred: false,
  active_task_group_ids: [],
  run_mode_lock: { locked: false },
  pendingUserInputs: [],
  collaboration: null,
  routing: null,
  currentPlan: null,
  activePlanRun: null,
  goal: null,
  goalSnapshotStreamSeq: 0,
  tasks: [],
  active_task: null,
  last_task: null,
  run_status: 'idle',
  hydration_complete: true,
  deferred_fields: [],
} as const

describe('generated Session read v4 Contracts', () => {
  it('accepts every characterized chat.history request, including legacy errors', () => {
    for (const testCase of historyFixture('requests.json').cases) {
      expect(
        historyValidators.validateChatHistoryRequestFrame(testCase.wire),
        testCase.id,
      ).toBe(true)
    }
  })

  it('accepts the complete characterized chat.history projections', () => {
    const completeCases = historyFixture('responses.json').cases
      .filter(testCase => Array.isArray(testCase.assert?.messages))

    for (const testCase of completeCases) {
      expect(historyValidators.validateChatHistoryResponseFrame({
        type: 'res',
        id: testCase.id,
        ok: true,
        payload: testCase.assert,
        error: null,
      }), testCase.id).toBe(true)
    }

    expect(historyValidators.validateChatHistoryResponseFrame({
      type: 'res',
      id: 'history-busy',
      ok: false,
      payload: null,
      error: {
        code: 'STORAGE_BUSY',
        message: 'Session storage is temporarily busy. Retry this operation.',
        retryable: true,
        retry_after_ms: 100,
        details: { operation: 'chat.history', stage: 'characterization' },
      },
    })).toBe(true)
  })

  it('accepts the real enriched and fast-ACK message subscription shapes', () => {
    const request = {
      type: 'req',
      id: 'message-subscribe',
      method: 'sessions.messages.subscribe',
      params: {
        key: 'agent:main:webchat:contract',
        since_stream_generation: 'generation-a',
        since_stream_seq: 7,
        fast_ack: true,
        future_option: 'preserved',
      },
    }
    expect(subscribeValidators.validateSessionsMessagesSubscribeRequestFrame(request)).toBe(true)

    expect(subscribeValidators.validateSessionsMessagesSubscribeResponseFrame({
      type: 'res',
      id: request.id,
      ok: true,
      payload: {
        subscribed: true,
        key: request.params.key,
        stream_generation: 'generation-a',
        current_stream_seq: 9,
        replay_complete: false,
        replay_gap_reason: 'cursor_too_old',
        replayed_count: 2,
        ...emptyMetadata,
        workspaceId: null,
        projectWorkspaceDeferred: true,
        run_mode_lock: { locked: true, source: 'deferred' },
        hydration_complete: false,
        deferred_fields: ['workspaceId', 'projectWorkspace', 'routing'],
      },
      error: null,
    })).toBe(true)
  })

  it('accepts authoritative hydration and live snapshot responses', () => {
    expect(hydrateValidators.validateSessionsMessagesHydrateRequestFrame({
      type: 'req',
      id: 'hydrate',
      method: 'sessions.messages.hydrate',
      params: { key: 'agent:main:webchat:contract' },
    })).toBe(true)
    expect(hydrateValidators.validateSessionsMessagesHydrateResponseFrame({
      type: 'res',
      id: 'hydrate',
      ok: true,
      payload: {
        key: 'agent:main:webchat:contract',
        ...emptyMetadata,
        epoch: 4,
      },
      error: null,
    })).toBe(true)

    expect(snapshotValidators.validateSessionsMessagesSnapshotRequestFrame({
      type: 'req',
      id: 'snapshot',
      method: 'sessions.messages.snapshot',
      params: { key: 'agent:main:webchat:contract' },
    })).toBe(true)
    expect(snapshotValidators.validateSessionsMessagesSnapshotResponseFrame({
      type: 'res',
      id: 'snapshot',
      ok: true,
      payload: {
        key: 'agent:main:webchat:contract',
        task_id: 'task-live',
        stream_generation: 'generation-a',
        current_stream_seq: 2,
        events: [{
          event: 'session.event.thinking',
          payload: {
            task_id: 'task-live',
            text: 'Inspecting',
            session_key: 'agent:main:webchat:contract',
            stream_generation: 'generation-a',
            stream_seq: 1,
            emitted_at: 100,
          },
        }],
      },
      error: null,
    })).toBe(true)
  })

  it('accepts null message-unsubscribe results and bounded previews', () => {
    expect(unsubscribeValidators.validateSessionsMessagesUnsubscribeRequestFrame({
      type: 'req',
      id: 'message-unsubscribe',
      method: 'sessions.messages.unsubscribe',
      params: { key: 'agent:main:webchat:contract' },
    })).toBe(true)
    expect(unsubscribeValidators.validateSessionsMessagesUnsubscribeResponseFrame({
      type: 'res',
      id: 'message-unsubscribe',
      ok: true,
      payload: null,
      error: null,
    })).toBe(true)

    expect(previewValidators.validateSessionsPreviewRequestFrame({
      type: 'req',
      id: 'preview',
      method: 'sessions.preview',
      params: {
        keys: ['agent:main:webchat:contract'],
        limit: 20,
      },
    })).toBe(true)
    expect(previewValidators.validateSessionsPreviewResponseFrame({
      type: 'res',
      id: 'preview',
      ok: true,
      payload: {
        ts: 1_000,
        previews: [{
          key: 'agent:main:webchat:contract',
          title: 'Contract session',
          lastMessage: 'Latest bounded message',
          updatedAt: null,
        }],
      },
      error: null,
    })).toBe(true)
  })

  it('keeps per-session message subscription distinct from directory subscription', () => {
    expect(subscribeValidators.validateSessionsMessagesSubscribeRequestFrame({
      type: 'req',
      id: 'directory-subscription',
      method: 'sessions.subscribe',
      params: null,
    })).toBe(false)
    expect(unsubscribeValidators.validateSessionsMessagesUnsubscribeRequestFrame({
      type: 'req',
      id: 'directory-unsubscribe',
      method: 'sessions.unsubscribe',
      params: null,
    })).toBe(false)
  })

  it('rejects incomplete success projections and cross-method frames', () => {
    expect(snapshotValidators.validateSessionsMessagesSnapshotResponseFrame({
      type: 'res',
      id: 'incomplete-snapshot',
      ok: true,
      payload: {
        key: 'agent:main:webchat:contract',
        events: [],
      },
    })).toBe(false)
    expect(previewValidators.validateSessionsPreviewRequestFrame({
      type: 'req',
      id: 'wrong-preview-method',
      method: 'sessions.resolve',
      params: { keys: [] },
    })).toBe(false)
  })
})
