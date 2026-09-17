import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ApprovalAvailability, ApprovalCenter, ApprovalSnapshot } from '@/modules/approvalCenter'
import type { GatewayAvailability } from '@/modules/gatewayAccess'
import type { InterruptViewState } from '@/types/parts'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { useChatApprovals } from './useChatApprovals'

const cleanups: Array<() => void> = []
afterEach(() => { while (cleanups.length) cleanups.pop()?.() })

async function flush() {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await nextTick()
}

function harness(initial: GatewayAvailability = 'preparing') {
  const scope = effectScope()
  const gatewayAvailability = ref(initial)
  const sessionKey = ref('first-session')
  const snapshot = vi.fn<ApprovalCenter['snapshot']>().mockResolvedValue({ mode: 'prompt', pending: [] })
  const appendInterruptFrame = vi.fn()
  const onSnapshotCount = vi.fn()
  let onAvailability: ((state: ApprovalAvailability) => void) | undefined
  const events = createConversationEventsTestHarness()
  const approvals = scope.run(() => useChatApprovals({
    gatewayAvailability,
    approvalCenter: {
      snapshot,
      subscribe: () => ({ close() {} }),
      subscribeAvailability: (listener: (state: ApprovalAvailability) => void) => {
        onAvailability = listener
        return { close: () => { onAvailability = undefined } }
      },
    } as unknown as ApprovalCenter,
    conversationEvents: events.events,
    clarificationSubmission: {} as never,
    sessionKey,
    runStatus: ref({ status: 'idle', label: '', task: null }),
    interruptState: ref<ReadonlyMap<string, InterruptViewState>>(new Map()),
    stream: { isStreaming: ref(false), appendInterruptFrame, ensureInterruptBubble: vi.fn() },
    onSnapshotCount,
  }))!
  const unsubscribe = approvals.subscribe()
  function close() { unsubscribe(); approvals.cleanup(); scope.stop() }
  cleanups.push(close)
  function connect(state: GatewayAvailability) {
    gatewayAvailability.value = state
    onAvailability?.(state === 'preparing' ? 'recovering' : state)
  }
  return { snapshot, approvals, sessionKey, connect, appendInterruptFrame, onSnapshotCount, close }
}

function pending(sessionKey: string): ApprovalSnapshot {
  return { mode: 'prompt', pending: [{
    id: 'approval-1', namespace: 'exec', sessionKey, toolName: 'shell', command: 'echo safe',
    approvalKind: '', args: null, warning: '', agent: 'main', deadline: 0,
  }] }
}

describe('chat approvals Gateway availability', () => {
  it('defers initial and session-switch hydration until ready, using the current session', async () => {
    const h = harness()
    h.sessionKey.value = 'current-session'
    await flush()
    expect(h.snapshot).not.toHaveBeenCalled()
    h.snapshot.mockResolvedValue(pending('current-session'))
    h.connect('available')
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(1)
    expect(h.approvals.approvalEntries.value[0]?.approval.sessionKey).toBe('current-session')
    expect(h.appendInterruptFrame).toHaveBeenCalledTimes(1)
  })

  it('does not hydrate while offline and refreshes on reconnect', async () => {
    const h = harness()
    h.connect('available')
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(1)
    h.connect('unavailable')
    h.sessionKey.value = 'other-session'
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(1)
    h.connect('available')
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(2)
    h.close()
    h.connect('unavailable')
    h.connect('available')
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(2)
  })

  it('rejects explicit reconciliation while offline without issuing a snapshot', async () => {
    const h = harness('unavailable')
    await expect(h.approvals.reconcile()).rejects.toThrow('Gateway is unavailable')
    expect(h.snapshot).not.toHaveBeenCalled()
  })

  it('discards the old connection snapshot and services the queued current-session refresh', async () => {
    const h = harness()
    let resolveOld!: (value: ApprovalSnapshot) => void
    h.snapshot.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    h.connect('available')
    h.connect('preparing')
    h.sessionKey.value = 'current-session'
    h.snapshot.mockResolvedValue(pending('current-session'))
    h.connect('available')
    resolveOld(pending('first-session'))
    await flush()
    expect(h.snapshot).toHaveBeenCalledTimes(2)
    expect(h.onSnapshotCount).toHaveBeenCalledTimes(1)
    expect(h.approvals.approvalEntries.value.map(entry => entry.approval.sessionKey)).toEqual(['current-session'])
    expect(h.appendInterruptFrame).toHaveBeenCalledTimes(1)
  })
})
