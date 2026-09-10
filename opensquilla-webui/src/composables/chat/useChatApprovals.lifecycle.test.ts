import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ConversationEventData } from '@/modules/conversationEventContent'
import type { ConversationEvent, ConversationSemanticEventKind } from '@/modules/conversationEvents'
import type { InterruptViewState } from '@/types/parts'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { useChatApprovals } from './useChatApprovals'

const cleanups: Array<() => void> = []
afterEach(() => { while (cleanups.length) cleanups.pop()?.() })

function deferred() {
  let resolve!: (value: unknown) => void
  let reject!: (error: Error) => void
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function questionnaire(requestId = 'request-1', taskId = 'task-1') {
  return {
    kind: 'user_input', paused: true, request_id: requestId, run_id: taskId, step: 'clarify',
    clarify_schema: {
      presentation: 'plan_questionnaire_v1', intro: 'Choose the scope.',
      fields: [{ name: 'scope', type: 'enum', required: true,
        choices: ['Focused', 'Complete'], prompt: 'Which scope?' }],
    },
  }
}

function harness() {
  const scope = effectScope()
  const events = createConversationEventsTestHarness()
  const sessionKey = ref('agent:main:web')
  const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
  const submit = vi.fn(async (_command: unknown): Promise<unknown> => ({ resolved: true }))
  const approvals = scope.run(() => useChatApprovals({
    approvalCenter: {
      snapshot: vi.fn(async () => ({ pending: [], mode: 'prompt' as const })),
      subscribe: vi.fn(() => ({ close: vi.fn() })),
      subscribeAvailability: vi.fn(() => ({ close: vi.fn() })),
    } as never,
    conversationEvents: events.events,
    clarificationSubmission: { submit } as never,
    sessionKey, interruptState,
    runStatus: ref({ status: 'running', label: '', task: { task_id: 'task-1' } }),
    stream: { isStreaming: ref(true), appendInterruptFrame: vi.fn(), ensureInterruptBubble: vi.fn() },
  }))!
  const unsubscribe = approvals.subscribe()
  cleanups.push(() => { unsubscribe(); scope.stop() })

  function emit(semanticKind: ConversationSemanticEventKind, payload: ConversationEventData = {}) {
    const data = { key: sessionKey.value, task_id: 'task-1', epoch: 1, ...payload }
    events.emit({
      kind: 'conversation', event: {
        kind: 'known', semanticKind, payload: data, meta: {},
        sessionKey: data.key, taskId: data.task_id, turnId: null,
        streamGeneration: data.stream_generation ?? null, streamSeq: data.stream_seq ?? null,
        connectionSeq: null, generationEpoch: null,
      },
    } as ConversationEvent)
  }
  function ask(requestId = 'request-1', taskId = 'task-1', extra: ConversationEventData = {}) {
    emit('tool-result', { task_id: taskId, id: `call-${requestId}`, name: 'request_user_input',
      approvalResult: questionnaire(requestId, taskId), ...extra })
  }
  return { approvals, interruptState, submit, sessionKey, events, emit, ask }
}

describe('request-scoped questionnaire lifecycle', () => {
  it.each([
    'task-timed-out', 'task-failed', 'task-cancelled', 'task-abandoned',
    'task-succeeded', 'turn-failed', 'turn-completed',
  ] as const)('expires the owning questionnaire on %s and rejects a late paused replay', event => {
    const h = harness()
    h.ask()
    h.emit(event, event === 'turn-completed' ? { reason: 'aborted' } : {})
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
    h.ask()
    expect(h.approvals.pendingClarify.value).toBeNull()
  })

  it('handles task terminal before the paused tool result arrives', () => {
    const h = harness()
    h.emit('task-cancelled')
    h.ask()
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
  })

  it('handles a matching terminal session-change projection', () => {
    const h = harness()
    h.ask()
    h.events.emit({ kind: 'sessions-changed', payload: {
      key: 'agent:main:web', epoch: 1, changed_task: { task_id: 'task-1', status: 'cancelled' },
    } })
    expect(h.approvals.pendingClarify.value).toBeNull()
  })

  it.each(['cancelled', 'failed'])('expires the matching last-task-only %s session projection', status => {
    const h = harness()
    h.ask()
    h.events.emit({ kind: 'sessions-changed', payload: {
      key: 'agent:main:web', epoch: 1, last_task: { task_id: 'task-1', status },
    } })
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
  })

  it('does not confuse a terminal last task with the running changed task', () => {
    const h = harness()
    h.ask('request-2', 'task-2')
    h.events.emit({ kind: 'sessions-changed', payload: {
      key: 'agent:main:web', epoch: 1,
      changed_task: { task_id: 'task-2', status: 'running' },
      last_task: { task_id: 'task-1', status: 'cancelled' },
    } })
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
  })

  it('ignores another session or task terminal and unowned terminal events', () => {
    const h = harness()
    h.ask('request-2', 'task-2')
    h.emit('task-cancelled', { key: 'agent:other:web', task_id: 'task-2' })
    h.emit('task-timed-out', { task_id: 'task-1' })
    h.emit('turn-completed', { task_id: undefined, reason: 'aborted' })
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
  })

  it.each(['answered', 'cancelled', 'expired'] as const)('preserves the meaning of the %s tool outcome', status => {
    const h = harness()
    h.ask()
    h.emit('tool-result', { approvalResult: {
      kind: 'user_input', paused: false, status, request_id: 'request-1',
    } })
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe(status === 'answered' ? 'replied' : 'expired')
  })

  it('expires an unsubmitted request missing from an authoritative snapshot but not a partial snapshot', () => {
    const h = harness()
    h.ask()
    h.approvals.applyUserInputBootstrap({})
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-1')
    h.approvals.applyUserInputBootstrap({ pendingUserInputs: [] })
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
    h.approvals.applyUserInputBootstrap({ pendingUserInputs: [questionnaire()] })
    expect(h.approvals.pendingClarify.value).toBeNull()
  })

  it('shows sending until acceptance and preserves it across duplicate paused events and snapshots', async () => {
    const h = harness()
    h.ask()
    const result = deferred()
    h.submit.mockImplementationOnce(() => result.promise)
    const submitted = h.approvals.submitClarify({ scope: 'Focused' })
    h.ask()
    h.approvals.applyUserInputBootstrap({ pendingUserInputs: [questionnaire()] })
    expect(h.approvals.clarifySubmitted.value).toBe(false)
    expect(h.approvals.clarifyBusy.value).toBe(true)
    expect(h.interruptState.value.get('request-1')).toMatchObject({ resolution: null, busy: true })
    result.resolve({ resolved: true })
    await submitted
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')).toMatchObject({ resolution: 'replied', busy: false })
  })

  it.each(['terminal', 'snapshot'] as const)('does not reopen an expired send after a late RPC failure (%s)', cause => {
    const h = harness()
    h.ask()
    const result = deferred()
    h.submit.mockImplementationOnce(() => result.promise)
    const submitted = h.approvals.submitClarify({ scope: 'Focused' })
    if (cause === 'terminal') h.emit('task-cancelled')
    else h.approvals.applyUserInputBootstrap({ pendingUserInputs: [] })
    result.reject(new Error('connection lost'))
    return submitted.then(() => {
      expect(h.approvals.pendingClarify.value).toBeNull()
      expect(h.interruptState.value.get('request-1')).toEqual({ resolution: 'expired', busy: false, error: '' })
    })
  })

  it.each([
    Object.assign(new Error('This question expired'), { code: 'USER_INPUT_EXPIRED' }),
    new Error('pending user-input request was not found'),
  ])('expires an authoritative missing-request rejection without reopening', async error => {
    const h = harness()
    h.ask()
    h.submit.mockRejectedValueOnce(error)
    await h.approvals.submitClarify({ scope: 'Focused' })
    expect(h.approvals.pendingClarify.value).toBeNull()
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
  })

  it.each(['resolve', 'reject'] as const)('does not affect a newer task question when an earlier submit settles: %s', async outcome => {
    const h = harness()
    h.ask()
    const result = deferred()
    h.submit.mockImplementationOnce(() => result.promise)
    const submitted = h.approvals.submitClarify({ scope: 'Focused' })
    h.emit('task-cancelled')
    h.ask('request-2', 'task-2')
    if (outcome === 'resolve') result.resolve({ resolved: true })
    else result.reject(new Error('late failure'))
    await submitted
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
    expect(h.approvals.clarifyBusy.value).toBe(false)
    expect(h.approvals.clarifySubmitted.value).toBe(false)
    expect(h.approvals.clarifyError.value).toBe('')
  })

  it('does not let an older metadata snapshot clear or replace a newer live questionnaire', () => {
    const h = harness()
    h.ask('request-2', 'task-2', { stream_seq: 20, stream_generation: 'generation-1' })
    h.approvals.applyUserInputBootstrap({ epoch: 1, streamSeq: 10, streamGeneration: 'generation-1', pendingUserInputs: [] })
    h.approvals.applyUserInputBootstrap({ epoch: 1, streamSeq: 10, streamGeneration: 'generation-1', pendingUserInputs: [questionnaire()] })
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
    h.approvals.applyUserInputBootstrap({ epoch: 1, streamSeq: 21, streamGeneration: 'generation-1', pendingUserInputs: [] })
    expect(h.approvals.pendingClarify.value).toBeNull()
  })

  it('ignores an older paused replay without replacing a newer question or its cursor', () => {
    const h = harness()
    h.ask('request-2', 'task-2', { stream_seq: 20, stream_generation: 'generation-1' })
    h.ask('request-1', 'task-1', { stream_seq: 5, stream_generation: 'generation-1' })
    h.ask('request-2', 'task-2', { stream_seq: 6, stream_generation: 'generation-1' })
    h.approvals.applyUserInputBootstrap({ streamSeq: 10, streamGeneration: 'generation-1', pendingUserInputs: [] })
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
  })

  it('fences a late submit and old events/snapshots after the session epoch changes', async () => {
    const h = harness()
    h.ask()
    const result = deferred()
    h.submit.mockImplementationOnce(() => result.promise)
    const submitted = h.approvals.submitClarify({ scope: 'Focused' })
    h.emit('session-epoch-changed', { epoch: 2 })
    h.ask('request-2', 'task-2', { epoch: 2 })
    h.approvals.applyUserInputBootstrap({ epoch: 1, pendingUserInputs: [] })
    h.ask('old-request', 'old-task', { epoch: 1 })
    result.resolve({ resolved: true })
    await submitted
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
    expect(h.interruptState.value.get('request-1')?.resolution).toBe('expired')
  })

  it('fences a late submit and foreign snapshot after switching sessions', async () => {
    const h = harness()
    h.ask()
    const result = deferred()
    h.submit.mockImplementationOnce(() => result.promise)
    const submitted = h.approvals.submitClarify({ scope: 'Focused' })
    h.sessionKey.value = 'agent:other:web'
    h.ask('request-2', 'task-2')
    h.approvals.applyUserInputBootstrap({ sessionKey: 'agent:main:web', pendingUserInputs: [] })
    result.reject(new Error('late failure'))
    await submitted
    expect(h.approvals.pendingClarify.value?.requestId).toBe('request-2')
    expect(h.interruptState.value.has('request-1')).toBe(false)
  })
})
