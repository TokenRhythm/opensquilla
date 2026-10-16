import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ApprovalAvailability, ApprovalCenter, ApprovalEvent, ApprovalItem } from '@/modules/approvalCenter'
import type { InterruptViewState } from '@/types/parts'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { useChatApprovals } from './useChatApprovals'

const cleanups: (() => void)[] = []
afterEach(() => { cleanups.splice(0).forEach(close => close()) })

function harness() {
  const events = createConversationEventsTestHarness()
  const open = vi.spyOn(events.events, 'open')
  const listeners = new Set<(event: ApprovalEvent) => void>()
  const availability = new Set<(state: ApprovalAvailability) => void>()
  const snapshot = vi.fn<ApprovalCenter['snapshot']>(async () => ({ mode: 'prompt', pending: [] }))
  const center = { snapshot,
    subscribe: (listener: (event: ApprovalEvent) => void) => { listeners.add(listener); return { close: () => listeners.delete(listener) } },
    subscribeAvailability: (listener: (state: ApprovalAvailability) => void) => { availability.add(listener); return { close: () => availability.delete(listener) } },
    status: vi.fn(), resolve: vi.fn(), extend: vi.fn(),
  } as unknown as ApprovalCenter
  function mount(key: string) {
    const scope = effectScope()
    const sessionKey = ref(key)
    const state = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
    const append = vi.fn()
    const submit = vi.fn()
    const api = scope.run(() => useChatApprovals({
      sessionKey, approvalCenter: center, conversationEvents: events.events, gatewayAvailability: ref('available'),
      clarificationSubmission: { submit } as never, interruptState: state,
      runStatus: ref({ status: 'idle', label: '', task: null }),
      stream: { isStreaming: ref(true), appendInterruptFrame: append, ensureInterruptBubble: vi.fn() },
    }))!
    const unsubscribe = api.subscribe()
    cleanups.push(() => { unsubscribe(); api.cleanup(); scope.stop() })
    return { api, sessionKey, state, append, submit }
  }
  function requested(id: string, key: string) {
    const approval: ApprovalItem = { id, sessionKey: key, namespace: 'exec', command: 'read file',
      toolName: 'read', approvalKind: '', args: null, warning: '', agent: 'main', deadline: 0 }
    const event: ApprovalEvent = { kind: 'requested', approvalId: id, namespace: 'exec', approval,
      sessionKey: key, approved: null, resolution: null, emittedAt: null, needsHydration: false }
    for (const listener of listeners) listener(event)
  }
  function resolved(id: string, key: string | null = null) {
    const event: ApprovalEvent = { kind: 'resolved', approvalId: id, namespace: 'exec', sessionKey: key,
      approved: true, resolution: 'approved', emittedAt: null, needsHydration: false }
    for (const listener of listeners) listener(event)
  }
  const ask = (key: string) => events.emitToolResult({ key, approvalResult: {
    kind: 'user_input', paused: true, request_id: 'question', run_id: 'task', step: 'clarify',
    clarify_schema: { intro: 'Choose', fields: [{ name: 'choice', prompt: 'Which?', type: 'string', required: true }] },
  } })
  return { mount, requested, resolved, ask, snapshot, listeners, availability, open }
}

describe('chat approval ownership', () => {
  it('does not subscribe, hydrate, accept bootstrap or submit a question without a session owner', async () => {
    const h = harness()
    const draft = h.mount('')
    expect(h.open).not.toHaveBeenCalled()
    expect(h.listeners.size).toBe(0)
    expect(h.availability.size).toBe(0)
    expect(h.snapshot).not.toHaveBeenCalled()
    draft.api.applyUserInputBootstrap({ sessionKey: 'parent', pendingUserInputs: [{
      kind: 'user_input', paused: true, request_id: 'question', run_id: 'task', step: 'clarify',
      clarify_schema: { intro: 'Choose', fields: [{ name: 'choice', prompt: 'Which?', type: 'string', required: true }] },
    }] })
    expect(draft.api.pendingClarify.value).toBeNull()
    expect(draft.append).not.toHaveBeenCalled()
    await draft.api.submitClarify({ choice: 'A' }, { intro: '', fields: [], requestId: 'question', runId: 'task', step: 'clarify' })
    expect(draft.submit).not.toHaveBeenCalled()
    await expect(draft.api.reconcile()).rejects.toThrow('Conversation identity is unavailable')

    draft.sessionKey.value = 'child'
    expect(h.open).toHaveBeenCalledExactlyOnceWith('child')
    expect(h.listeners.size).toBe(1)
    h.ask('parent')
    expect(draft.api.pendingClarify.value).toBeNull()
    h.ask('child')
    expect(draft.api.pendingClarify.value?.requestId).toBe('question')
  })

  it('keeps global approval resolutions confined to the main or side request that already owns them', () => {
    const h = harness()
    const main = h.mount('parent')
    const side = h.mount('child')
    const empty = h.mount('')
    h.requested('main-approval', 'parent')
    h.requested('side-approval', 'child')
    expect(main.append).toHaveBeenCalledTimes(1)
    expect(side.append).toHaveBeenCalledTimes(1)
    expect(empty.append).not.toHaveBeenCalled()
    h.resolved('unknown')
    expect(main.state.value.has('unknown')).toBe(false)
    expect(side.state.value.has('unknown')).toBe(false)
    h.resolved('main-approval', 'child')
    expect(main.state.value.get('main-approval')?.resolution).toBeNull()
    h.resolved('main-approval')
    expect(main.state.value.get('main-approval')?.resolution).toBe('approved')
    expect(side.state.value.has('main-approval')).toBe(false)
    h.resolved('side-approval')
    expect(side.state.value.get('side-approval')?.resolution).toBe('approved')
    expect(main.state.value.has('side-approval')).toBe(false)
    expect(empty.state.value.size).toBe(0)
  })

  it('retires all listeners on child-to-empty and fences a previously captured callback', () => {
    const h = harness()
    const side = h.mount('child')
    const late = [...h.listeners][0]
    h.requested('old', 'child')
    side.sessionKey.value = ''
    expect(h.listeners.size).toBe(0)
    expect(h.availability.size).toBe(0)
    late({ kind: 'resolved', approvalId: 'old', namespace: 'exec', sessionKey: 'child', approved: true,
      resolution: 'approved', needsHydration: false, emittedAt: null })
    h.ask('child')
    expect(side.state.value.size).toBe(0)
    expect(side.api.pendingClarify.value).toBeNull()
    side.sessionKey.value = 'next-child'
    h.requested('new', 'next-child')
    expect(side.state.value.has('new')).toBe(true)
    expect(side.state.value.has('old')).toBe(false)
  })
})
