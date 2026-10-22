// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, createApp, effectScope, h, nextTick, ref, watch } from 'vue'
import CompactionEvent from '@/components/chat/CompactionEvent.vue'
import chatViewSource from '@/views/ChatView.vue?raw'
import i18n from '@/i18n'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import { projectInitialCompactionRows } from '@/utils/chat/compactionPresentation'
import { resolveChatHistoryRecoveryState, visibleChatHistoryRecoveryState } from '@/utils/chat/sessionLoadState'
import { SessionReadFailure, type SessionReadHistoryPage, type SessionReadLease } from '@/modules/sessionReadLifecycle'
import { useChatCompaction } from './useChatCompaction'
import { useChatHistory } from './useChatHistory'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatSessionViewCache } from './useChatSessionViewCache'

// Execute the composition root's actual projection and maintenance watcher.
// A helper-only test would miss a disconnected gate or a lost terminal event.
const projection = chatViewSource.match(/const findDisplayMessages = computed\([\s\S]*?(?=\n\nfunction shouldRenderRouterStrip)/)?.[0]
const maintenance = chatViewSource.match(/function transcriptCompactionState\([\s\S]*?(?=\n\nconst chatUsageWidget)/)?.[0]
if (!projection || !maintenance) throw new Error('ChatView compaction presentation wiring is missing')
const project = new Function('computed', 'findContextMessages', 'findContextRendered', 'visibleRenderedMessages',
  'projectInitialCompactionRows', 'sessionKey', 'historySessionKey', 'historyState', 'sessionViewCache', 'compactStatus',
  `${projection}; return findDisplayMessages`)
const installMaintenance = new Function('watch', 'compactStatus', 'messages', maintenance
  .replace('(status: string): ChatMaintenanceEvent[\'state\']', '(status)')
  .replace('const maintenance: ChatMaintenanceEvent', 'const maintenance')
  .replace('messages.value[index]!', 'messages.value[index]'))
const terminalWiring = chatViewSource.match(/showCompactionToast: (\(payload, meta\) => \{[\s\S]*?)\n  },\n  getCompactionPlacement/)?.[1]
const epochWiring = chatViewSource.match(/onReadEpoch: (\(key, epoch\) => \{[\s\S]*?\n  }),\n  onReadStarted:/)?.[1]
if (!terminalWiring || !epochWiring) {
  throw new Error('ChatView terminal receipt/epoch wiring is missing')
}
const wireTerminals = new Function('sessionKey', 'sessionReadLifecycle', 'deliveryIdentity',
  'showCompactionToast', 'sessionViewCache', `return ${terminalWiring}\n}`)
const wireReadEpoch = new Function('sessionViewCache', 'scheduleHistorySync', `return ${epochWiring}`)

const ID = 'cmp-cold-reload'
function page(empty = false): SessionReadHistoryPage {
  return {
    messages: empty ? [] : [{
      id: 'user-a', messageId: 'user-a', transcriptId: null, role: 'user', text: 'Keep my query.',
      createdAt: Date.now() - 1000, reasoningContent: null, routerDecision: null,
      artifacts: [], toolCalls: [], timeline: [], attachments: [], promptAnnotations: [],
      provenance: { kind: null, sourceSessionKey: null, sourceTool: null }, turnContext: null,
      usage: null, model: null, inputTokens: null, outputTokens: null, additional: {},
    }],
    hasMore: false, oldestCursor: null, newestCursor: null, scope: 'complete', loadedCount: empty ? 0 : 1,
    pageSize: 50, canonicalAvailable: true, canonicalComplete: true,
    compactionSummaries: [], turnOutcomes: [], additional: {},
  }
}

function harness() {
  const scope = effectScope()
  const sessionKey = ref('A')
  const messages = ref<ChatMessage[]>([])
  const schedulePendingDrainAfterTerminal = vi.fn()
  const scheduleHistorySync = vi.fn()
  const readHistory = vi.fn<() => Promise<SessionReadHistoryPage>>().mockResolvedValue(page())
  const lease = { criticalRequestsQueued: Promise.resolve(), history: {
    latest: readHistory, before: readHistory, after: readHistory,
  } } as unknown as SessionReadLease
  const state = scope.run(() => {
    const deliveryIdentity = ref('gateway:owner')
    const reader = { current: () => lease }
    const cache = useChatSessionViewCache({ sessionKey, messages, deliveryIdentity, waitForLive: true,
      currentReadAuthority: reader.current })
    const history = useChatHistory({ sessionKey, messages,
      sessionReadLeaseReader: { current: () => lease }, lastHeaderRole: ref(''), lastHeaderDay: ref(''),
      stripTimePrefix: text => text, scrollToBottom: vi.fn(),
      onHistoryInstalled: (key, compactions) => cache.historyInstalled(key, true, compactions),
    })
    const compaction = useChatCompaction({ sessionKey, schedulePendingDrainAfterTerminal, popAllPendingIntoComposer: () => true })
    const receive = wireTerminals(sessionKey, reader, deliveryIdentity, compaction.showCompactionToast,
      cache) as typeof compaction.showCompactionToast
    const receiveEpoch = wireReadEpoch(cache, scheduleHistorySync) as (key: string, epoch: number | null) => void
    installMaintenance(watch, compaction.compactStatus, messages)
    const rendered = useChatRenderedMessages({
      messages: cache.messages, sessionKey, isPreview: cache.showingPreview,
      routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
    })
    const display = project(computed, ref(null), { renderedMessages: ref([]) }, rendered.renderedMessages,
      projectInitialCompactionRows, sessionKey, history.historySessionKey, history.historyState,
      cache, compaction.compactStatus) as Readonly<{ value: ChatRenderedMessage[] }>
    return { cache, history, compaction, display, receive, receiveEpoch }
  })!
  const element = document.createElement('div')
  const app = createApp({ render: () => h('div', state.display.value.map((message, index) =>
    h('div', { 'data-index': index }, message.maintenance
      ? [h(CompactionEvent, { message, preview: state.cache.showingPreview.value })]
      : [h('span', { 'data-user-id': message.messageId }, message.text)]),
  )) })
  app.use(i18n)
  app.mount(element)
  const start = () => state.compaction.showCompactionToast({
    status: 'observed', source: 'manual', compaction_id: ID, sequence: 3,
  }, { authoritativeLive: true })
  const deferredRead = (retry = false) => {
    let resolve!: (value: SessionReadHistoryPage) => void
    let reject!: (error: Error) => void
    readHistory.mockImplementationOnce(() => new Promise((done, fail) => { resolve = done; reject = fail }))
    const pending = retry ? state.history.retryHistory() : state.history.loadHistory()
    return { pending, resolve, reject }
  }
  return { ...state, sessionKey, messages, element, start, readHistory, deferredRead,
    schedulePendingDrainAfterTerminal, scheduleHistorySync,
    stop: () => { app.unmount(); state.history.cleanup(); scope.stop() },
  }
}

afterEach(() => vi.useRealTimers())

describe('initial compaction transcript presentation', () => {
  it('does not borrow the departed session\'s error/retry display while a cold session has no history binding', async () => {
    const h = harness()
    try {
      const read = h.deferredRead()
      read.reject(new SessionReadFailure('busy', 'History permit busy.', true))
      await read.pending
      const retry = h.deferredRead(true)
      h.sessionKey.value = 'B'
      h.messages.value = []
      h.compaction.showCompactionToast({ status: 'observed', source: 'manual',
        compaction_id: 'cmp-session-b', sequence: 1 }, { authoritativeLive: true })
      await nextTick()
      expect(h.history.historySessionKey.value).toBe('A')
      expect(h.history.historyState.value.retrying).toBe(true)
      expect(h.element.querySelector('[data-testid="compaction-event"]')).toBeNull()
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(true)
      retry.resolve(page())
      await retry.pending
    } finally { h.stop() }
  })

  it('waits for a cold history anchor without changing the accepted operation, then renders it once after the user', async () => {
    const h = harness()
    try {
      const read = h.deferredRead()
      h.start()
      h.cache.liveInstalled('A')
      await nextTick()
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.compaction.compactStatus.value.compactionId).toBe(ID)
      expect(h.messages.value).toHaveLength(1)
      expect(h.element.querySelector('[data-testid="compaction-event"]')).toBeNull()
      read.resolve(page())
      expect(await read.pending).toMatchObject({ ok: true })
      await nextTick()
      const row = h.element.querySelector('[data-testid="compaction-event"]')
      expect(row?.getAttribute('data-compaction-id')).toBe(ID)
      expect(row?.classList.contains('chat-compaction-event--running')).toBe(true)
      expect(row?.parentElement?.getAttribute('data-index')).toBe('1')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      expect(h.element.querySelectorAll('[data-testid="compaction-event"]')).toHaveLength(1)
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(true)
    } finally { h.stop() }
  })

  it('releases the display gate on a cold history error and keeps the live operation and recovery visible', async () => {
    const h = harness()
    try {
      const read = h.deferredRead()
      h.start()
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')).toBeNull()
      read.reject(new SessionReadFailure('busy', 'History permit busy.', true))
      expect(await read.pending).toMatchObject({ ok: false })
      await nextTick()
      expect(h.history.historyState.value.initialLoadStatus).toBe('error')
      expect(visibleChatHistoryRecoveryState(resolveChatHistoryRecoveryState({ isDraftLanding: false,
        initialHistoryStatus: h.history.historyState.value.initialLoadStatus,
        retrying: h.history.historyState.value.retrying, recoveryError: h.history.historyState.value.recoveryError,
      }))).toBe('history-error')
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.getAttribute('data-status')).toBe('running')
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.compaction.compactStatus.value.compactionId).toBe(ID)
      // An error is a settled degraded display. A retry must not withdraw the
      // operation again while it is trying to recover the missing history.
      const retry = h.deferredRead(true)
      expect(h.history.historyState.value).toMatchObject({ initialLoadStatus: 'loading', retrying: true })
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.getAttribute('data-status')).toBe('running')
      retry.resolve(page())
      await retry.pending
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.parentElement?.getAttribute('data-index')).toBe('1')
    } finally { h.stop() }
  })

  it('retains and settles a terminal received before history finishes, then reveals that same terminal at its anchor', async () => {
    const h = harness()
    try {
      const read = h.deferredRead()
      h.start()
      h.compaction.showCompactionToast({ status: 'completed', source: 'manual', compaction_id: ID, sequence: 4 })
      await nextTick()
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      expect(h.messages.value[0]?.maintenance).toMatchObject({ compactionId: ID, state: 'completed' })
      expect(h.element.querySelector('[data-testid="compaction-event"]')).toBeNull()
      read.resolve(page())
      await read.pending
      await nextTick()
      const row = h.element.querySelector('[data-testid="compaction-event"]')
      expect(row?.getAttribute('data-status')).toBe('completed')
      expect(row?.parentElement?.getAttribute('data-index')).toBe('1')
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally { h.stop() }
  })

  it('keeps an installed row visible during a later history refresh', async () => {
    const h = harness()
    try {
      await h.history.loadHistory()
      h.cache.liveInstalled('A')
      h.start()
      await nextTick()
      const read = h.deferredRead()
      expect(h.history.historyState.value).toMatchObject({ loading: true, initialLoadStatus: 'ready' })
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.parentElement?.getAttribute('data-index')).toBe('1')
      read.resolve(page())
      await read.pending
    } finally { h.stop() }
  })

  it('keeps the anchored warm preview visible while its session history key and initial load catch up', async () => {
    const h = harness()
    try {
      await h.history.loadHistory()
      h.start()
      h.cache.liveInstalled('A')
      h.sessionKey.value = 'B'
      h.messages.value = []
      await h.history.loadHistory()
      h.cache.liveInstalled('B')
      h.sessionKey.value = 'A'
      h.messages.value = []
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.history.historySessionKey.value).toBe('B')
      h.start()
      const read = h.deferredRead()
      await nextTick()
      expect(h.history.historyState.value.initialLoadStatus).toBe('loading')
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.parentElement?.getAttribute('data-index')).toBe('1')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      read.resolve(page())
      await read.pending
      h.cache.liveInstalled('A')
      await nextTick()
      expect(h.element.querySelectorAll('[data-testid="compaction-event"]')).toHaveLength(1)
    } finally { h.stop() }
  })

  it('releases index zero after history confirms that the session is empty', async () => {
    const h = harness()
    try {
      const read = h.deferredRead()
      h.start()
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')).toBeNull()
      read.resolve(page(true))
      await read.pending
      await nextTick()
      expect(h.history.historyState.value.initialLoadStatus).toBe('ready')
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.parentElement?.getAttribute('data-index')).toBe('0')
    } finally { h.stop() }
  })
})

async function warmPreview() {
  const h = harness()
  h.receiveEpoch('A', 0)
  await h.history.loadHistory()
  h.start()
  h.cache.liveInstalled('A')
  h.sessionKey.value = 'B'
  h.messages.value = []
  h.compaction.setCompactInFlight(false)
  h.compaction.hideCompactStatus()
  await h.history.loadHistory()
  h.cache.liveInstalled('B')
  h.sessionKey.value = 'A'
  h.messages.value = []
  // The subscription invokes this fast proof under its active lease before
  // replaying the snapshot, while deferred metadata may still be unavailable.
  h.receiveEpoch('A', 0)
  await nextTick()
  return h
}

describe('early read epoch composition', () => {
  it('invalidates a changed epoch and schedules replacement history before metadata arrives', async () => {
    const h = await warmPreview()
    try {
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.scheduleHistorySync).not.toHaveBeenCalled()
      const generation = h.cache.generation.value
      h.receiveEpoch('A', 1)
      expect(h.cache.generation.value).toBe(generation + 1)
      expect(h.cache.showingPreview.value).toBe(false)
      expect(h.cache.ready.value).toBe(false)
      expect(h.scheduleHistorySync).toHaveBeenCalledOnce()
      // The later observation of this same epoch cannot schedule a second read.
      h.receiveEpoch('A', 1)
      expect(h.cache.generation.value).toBe(generation + 1)
      expect(h.scheduleHistorySync).toHaveBeenCalledOnce()
    } finally { h.stop() }
  })

  it.each([
    ['same epoch', 'A', 0],
    ['unknown epoch', 'A', null],
    ['inactive session', 'B', 1],
  ] as const)('keeps the warm view without scheduling history for %s', async (_label, key, epoch) => {
    const h = await warmPreview()
    try {
      const generation = h.cache.generation.value
      h.receiveEpoch(key, epoch)
      expect(h.cache.generation.value).toBe(generation)
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.cache.messages.value[0]?.messageId).toBe('user-a')
      expect(h.scheduleHistorySync).not.toHaveBeenCalled()
    } finally { h.stop() }
  })
})

describe('warm compaction receipt composition', () => {
  it('labels an unverified cached running receipt as recovering without deleting its query or inventing completion', async () => {
    const h = await warmPreview()
    try {
      const row = h.element.querySelector('[data-testid="compaction-event"]')
      expect(row?.getAttribute('data-status')).toBe('recovering')
      expect(row?.getAttribute('data-freshness')).toBe('last-known')
      expect(row?.classList.contains('chat-compaction-event--running')).toBe(false)
      expect(row?.textContent).toContain(String(i18n.global.t('chat.compact.recovering')))
      expect(h.cache.messages.value[1]?.maintenance?.state).toBe('running')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      expect(h.cache.ready.value).toBe(false)
    } finally { h.stop() }
  })

  it.each([false, true])('patches a validated terminal at the cached anchor before metadata (history first=%s)', async historyFirst => {
    const h = await warmPreview()
    try {
      if (historyFirst) await h.history.loadHistory()
      h.start()
      const payload = { key: 'A', status: 'completed', source: 'manual', compaction_id: ID, sequence: 4 }
      expect(h.receive(payload)).toBe('standalone')
      await nextTick()
      const row = h.element.querySelector('[data-testid="compaction-event"]')
      expect(row?.getAttribute('data-status')).toBe('completed')
      expect(row?.parentElement?.getAttribute('data-index')).toBe('1')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      expect(h.element.querySelectorAll('[data-testid="compaction-event"]')).toHaveLength(1)
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.cache.ready.value).toBe(false)
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      expect(h.receive(payload)).toBe(false)
      expect(h.receive({ ...payload, status: 'observed', sequence: 5 })).toBe(false)
      expect(h.receive({ ...payload, status: 'failed', sequence: 6 })).toBe(false)
      await nextTick()
      expect(row?.getAttribute('data-status')).toBe('completed')
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally { h.stop() }
  })

  it('reconciles canonical summary-only completion before hydrate while retaining the cached history', async () => {
    const h = await warmPreview()
    try {
      const read = h.deferredRead()
      const canonical = page()
      read.resolve({ ...canonical, compactionSummaries: [{
        id: 'summary-1', compactionId: ID, compactionIndex: 1, triggerReason: 'manual',
        summaryText: '', summaryFormat: 'standard', coverageStatus: 'pass', removedCount: 1, keptCount: 1,
        coveredThroughId: 'user-a', createdAt: Date.now(), additional: {},
      }] })
      await read.pending
      await nextTick()
      const row = h.element.querySelector('[data-testid="compaction-event"]')
      expect(row?.getAttribute('data-status')).toBe('completed')
      expect(row?.parentElement?.getAttribute('data-index')).toBe('1')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      expect(h.cache.showingPreview.value).toBe(true)
      expect(h.cache.ready.value).toBe(false)
      expect(h.compaction.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    } finally { h.stop() }
  })

  it('keeps a history failure readable and rejects the inactive session terminal', async () => {
    const h = await warmPreview()
    try {
      const read = h.deferredRead()
      read.reject(new SessionReadFailure('busy', 'History permit busy.', true))
      await read.pending
      expect(h.receive({ key: 'B', source: 'manual', status: 'completed', compaction_id: ID, sequence: 4 })).toBe(false)
      await nextTick()
      expect(h.element.querySelector('[data-testid="compaction-event"]')?.getAttribute('data-status')).toBe('recovering')
      expect(h.element.querySelector('[data-user-id="user-a"]')).not.toBeNull()
      expect(h.cache.showingPreview.value).toBe(true)
    } finally { h.stop() }
  })
})
