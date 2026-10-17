// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, onUnmounted, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import ConversationLiveEdge from '@/components/chat/ConversationLiveEdge.vue'
import type { ApprovalCenter, ApprovalEvent, ApprovalItem } from '@/modules/approvalCenter'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import type { ChatPart, InterruptViewState } from '@/types/parts'
import { projectAssistantActivity } from '@/utils/chat/assistantActivity'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { clarificationSubmissionFromTestRpc } from '@/testing/conversationAncillary.test-helper'
import { useChatApprovals } from './useChatApprovals'
import { useChatStream } from './useChatStream'

const apps: App[] = []
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(100_000)
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(Date.now()), 16))
  vi.stubGlobal('cancelAnimationFrame', clearTimeout)
  i18n.global.locale.value = 'en'
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
const flush = async () => { await vi.advanceTimersByTimeAsync(50); await nextTick() }

async function harness(hydrate = false) {
  const item: ApprovalItem = { id: 'approval', namespace: 'exec', toolName: 'exec_command',
    command: 'Write-Output marker', displayKind: 'run_command', displayTarget: 'Write-Output marker',
    approvalKind: '', args: null, warning: '', agent: 'main',
    sessionKey: 'session', deadline: 0 }
  const listeners = new Set<(event: ApprovalEvent) => void>()
  const center: ApprovalCenter = {
    setElevatedMode: vi.fn(), snapshot: vi.fn(async () => ({ mode: 'prompt' as const, pending: hydrate ? [item] : [] })),
    status: vi.fn(), extend: vi.fn(), dispose: vi.fn(),
    resolve: vi.fn(async () => ({ resolved: true, pending: false, approved: false, resolution: 'denied' })),
    subscribe: listener => { listeners.add(listener); return { close: () => listeners.delete(listener) } },
    subscribeAvailability: () => ({ close: vi.fn() }),
  }
  const messages = ref<ChatMessage[]>([])
  const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
  const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
  const applySessionRunState = vi.fn()
  let stream!: ReturnType<typeof useChatStream>
  let approvals!: ReturnType<typeof useChatApprovals>
  const host = document.createElement('div'); document.body.appendChild(host)
  const renderMarkdown = (text: string) => text
  const app = createApp({ setup() {
    stream = useChatStream({ messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false),
      runStatus, applySessionRunState, renderMarkdown, stripDirectiveTags: text => text,
      stripGeneratedArtifactMarkers: text => text, scrollToBottom: vi.fn(), interruptState })
    approvals = useChatApprovals({ gatewayAvailability: ref('available'), approvalCenter: center,
      conversationEvents: createConversationEventsTestHarness().events,
      clarificationSubmission: clarificationSubmissionFromTestRpc({ call: vi.fn() }),
      sessionKey: ref('session'), runStatus, stream, interruptState })
    const unsubscribe = approvals.subscribe()
    onUnmounted(() => { unsubscribe(); approvals.cleanup(); stream.cleanup() })
    return () => h(ConversationLiveEdge, {
      visible: stream.isStreaming.value && stream.streamBubble.value, sessionKey: 'session', continuityKey: 'turn',
      executing: stream.isStreaming.value, stepCount: 0, failureCount: 0, phaseLabel: '',
      elapsedLabel: stream.streamTurnElapsed.value, stale: false, hasUnifiedActivityOrder: true,
      displayProjection: projectAssistantActivity({ role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant',
        text: '', timeStr: '', showHeader: false, timelineItems: [] }, renderMarkdown, []),
      pendingInterruptParts: stream.foldedTurn.value.parts.filter(
        (part): part is Extract<ChatPart, { type: 'interrupt' }> => part.type === 'interrupt' && !part.resolution),
      renderMarkdown, isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
      toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      onResolve: (id, decision) => approvals.resolveInterrupt(id, decision),
    })
  } }).use(i18n).use(createPinia())
  apps.push(app); app.mount(host); await flush()
  const request = async () => {
    listeners.forEach(listener => listener({ kind: 'requested', approvalId: item.id, namespace: 'exec',
      approval: item, sessionKey: item.sessionKey, approved: null, resolution: null,
      emittedAt: Date.now(), needsHydration: false }))
    await flush()
  }
  return { host, stream, messages, runStatus, approvals, request, center, applySessionRunState }
}

describe('approval presentation and execution ownership', () => {
  it.each([false, true])('renders and resolves a standalone %s approval without inventing a run', async hydrate => {
    const state = await harness(hydrate)
    if (!hydrate) await state.request()
    expect(state.host.textContent).toContain('Write-Output marker')
    expect(state.host.querySelector('.conversation-live-edge')).toBeNull()
    expect(state.stream.isStreaming.value).toBe(false)
    expect(state.applySessionRunState).not.toHaveBeenCalled()
    const deny = [...state.host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Deny')
    expect(deny).toBeDefined()
    deny!.click(); await flush()
    expect(state.center.resolve).toHaveBeenCalledWith({ namespace: 'exec', id: 'approval', decision: 'deny' })
    await vi.advanceTimersByTimeAsync(34_000)
    expect(state.host.querySelector('.conversation-live-edge')).toBeNull()
    expect(state.host.textContent).toBe('')
    expect(state.stream.isStreaming.value).toBe(false)
    expect(state.messages.value).toEqual([])
  })

  it('keeps a recovered pending card when real execution starts, then settles the actual turn normally', async () => {
    const state = await harness(true)
    state.stream.startStreaming()
    await flush()
    expect(state.host.textContent).toContain('Write-Output marker')
    expect(state.host.querySelectorAll('.conversation-live-edge')).toHaveLength(1)
    await state.approvals.resolveInterrupt('approval', 'deny'); await flush()
    expect(state.stream.isStreaming.value).toBe(true)
    state.stream.appendDelta('Denied command was not run.')
    state.stream.endStreaming()
    expect(state.stream.isStreaming.value).toBe(false)
    expect(state.messages.value).toHaveLength(1)
    expect(state.messages.value[0].text).toBe('Denied command was not run.')
    expect(state.messages.value[0].interrupts).toMatchObject([{ resolution: 'denied' }])
  })

  it('does not attach a settled standalone approval to a subsequent user turn', async () => {
    const state = await harness(true)
    await state.approvals.resolveInterrupt('approval', 'deny'); await flush()
    state.stream.startStreaming()
    expect(state.stream.foldedTurn.value.parts.filter(part => part.type === 'interrupt')).toEqual([])
    state.stream.appendDelta('New answer')
    state.stream.endStreaming()
    expect(state.messages.value[0].interrupts).toEqual([])
  })
})
