import { describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { requestV2SessionHistory } from './sessionReadV2'
import { requestV4SessionHistory } from './sessionHistoryV4'
import { useChatHistory } from '@/composables/chat/useChatHistory'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { reconcileHistoryMessages } from '@/utils/chat/historyMerge'
import { projectAssistantActivity } from '@/utils/chat/assistantActivity'
import type { ChatHistoryMessage, ChatHistoryResult } from '@/contracts/generated/v4/chatHistory'
import type { HistoryItem, Result } from '@/contracts/generated/v4/sessionsHistoryPageV2'
import type { ChatMessage } from '@/types/chat'
import type { SessionReadHistoryPage, SessionReadLease } from '@/modules/sessionReadLifecycle'

const key = 'agent:main:webchat:synthetic-semantic-audit'
const signal = new AbortController().signal
const answer = 'Final answer.\n' + 'A complete ordinary reply. '.repeat(180)
const message: ChatHistoryMessage = {
  id: 'answer', message_id: 'answer', transcript_id: 'answer', role: 'assistant',
  text: `Working note.\n\n${answer}`, timestamp: 1000,
  reasoning_content: 'Measured reasoning', artifacts: [{ id: 'artifact-synthetic' }],
  timeline: [
    { type: 'text', raw: 'Working note.', presentation: 'intermediate' },
    { type: 'text', raw: answer, presentation: 'answer' },
  ],
  turn_context: { turn_id: 'turn' }, usage: { input_tokens: 10 },
}

function wireItem(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    message_id: 'answer', item_id: 'answer', order: '2', role: 'assistant',
    preview: message.text!, preview_complete: true, contents: [],
    message, source_revision: 'r1', content_availability: 'ready', ...overrides,
  }
}

async function readV2(items = [wireItem()], overrides: Partial<Result> = {}) {
  const result: Result = {
    session_id: 'sid', session_epoch: 0, projection_revision: 2,
    before_cursor: null, after_cursor: null, has_more_before: false, has_more_after: false,
    complete_for_requested_window: true, compaction_summaries: [], turn_outcomes: [],
    canonical_available: true, canonical_complete: true, history_scope: 'complete',
    items, ...overrides,
  }
  return requestV2SessionHistory({ generation: 1, request: vi.fn().mockResolvedValue(result) },
    key, { direction: 'latest', limit: 50, signal }, 1)
}

async function readV4(messages: ChatHistoryMessage[], overrides: Partial<ChatHistoryResult> = {}) {
  const result: ChatHistoryResult = {
    messages, has_more: false, oldest_cursor: null, newest_cursor: null, history_scope: 'complete',
    loaded_count: messages.length, page_size: 50, canonical_available: true, canonical_complete: true,
    compaction_summaries: [], turn_outcomes: [], ...overrides,
  }
  return requestV4SessionHistory({ request: vi.fn().mockResolvedValue(result) },
    key, { direction: 'latest', limit: 50, signal }, {
      includeSummaries: true, policy: { concurrentHistoryReads: () => true }, contractError: value => new Error(value),
    })
}

async function install(page: SessionReadHistoryPage) {
  const scope = effectScope()
  const messages = ref<ChatMessage[]>([])
  const lease = { history: { latest: async () => page } } as SessionReadLease
  const api = scope.run(() => useChatHistory({
    sessionReadLeaseReader: { current: () => lease }, sessionKey: ref(key), messages,
    lastHeaderRole: ref(''), lastHeaderDay: ref(''), stripTimePrefix: value => value, scrollToBottom: () => {},
  }))!
  try {
    await api.loadHistory()
    const rendered = useChatRenderedMessages({
      messages, sessionKey: ref(key), routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
      renderMarkdown: value => value, stripGeneratedArtifactMarkers: value => value,
      stripTimePrefix: value => value, isSubagentCompletionMessage: () => false,
    }).renderedMessages.value
    return { messages: messages.value, rendered, state: { ...api.historyState.value } }
  } finally { api.cleanup(); scope.stop() }
}

describe('v2 history semantic fidelity', () => {
  it.each([true, false])('keeps the existing v4 fallback policy when the page is empty=%s', async empty => {
    const proof = { canonical_available: false, canonical_complete: false, history_scope: 'latest_window' as const }
    const page = await readV2(empty ? [] : [wireItem()], proof)
    const installed = await install(page)
    const legacy = await install(await readV4(empty ? [] : [message], proof))
    expect(installed.state).toEqual(legacy.state)
    expect(installed.messages.map(row => row.text)).toEqual(legacy.messages.map(row => row.text))
    expect(installed.state).toMatchObject({
      canonicalAvailable: false, canonicalComplete: false, historyScope: 'latestWindow',
      initialLoadStatus: empty ? 'error' : 'ready', recoveryError: empty,
    })
  })

  it('keeps an available old archive readable without claiming complete canonical coverage', async () => {
    const page = await readV2([wireItem()], {
      canonical_available: true, canonical_complete: false, history_scope: 'compacted',
    })
    const installed = await install(page)
    expect(installed.messages[0]?.text).toBe(message.text)
    expect(installed.state).toMatchObject({
      canonicalAvailable: true, canonicalComplete: false, historyScope: 'compacted',
      initialLoadStatus: 'ready', recoveryError: false,
    })
  })

  it('settles a positively confirmed empty draft', async () => {
    const installed = await install(await readV2([], {
      canonical_available: false, canonical_complete: true, history_scope: 'complete',
    }))
    expect(installed.messages).toEqual([])
    expect(installed.state).toMatchObject({
      canonicalAvailable: false, canonicalComplete: true,
      initialLoadStatus: 'ready', recoveryError: false,
    })
  })

  it('does not confuse byte-limited window completeness with canonical coverage or history scope', async () => {
    const page = await readV2([wireItem()], {
      complete_for_requested_window: false, has_more_before: true, before_cursor: 'older-page',
      canonical_available: true, canonical_complete: true, history_scope: 'complete',
    })
    expect(page).toMatchObject({
      canonicalAvailable: true, canonicalComplete: true, scope: 'complete', hasMore: true,
      additional: { completeForRequestedWindow: false },
    })
  })

  it('renders a complete ordinary answer and its presentation without loading stored JSON', async () => {
    const installed = await install(await readV2())
    const row = installed.rendered[0]!
    expect(row).toMatchObject({ previewComplete: true, contentRevision: 'r1', artifacts: message.artifacts })
    expect(row.text).toBe(message.text)
    const projection = projectAssistantActivity(row, value => value)
    expect(projection.answerPart?.rawText).toBe(answer)
    expect(projection.activityItems).toMatchObject([{ type: 'text', rawText: 'Working note.' }])
    expect(projection.answerPart?.rawText).not.toContain('\\n')
  })

  it.each(['preparing', 'unavailable'] as const)('preserves incomplete %s body state independently of a complete window', async availability => {
    const page = await readV2([wireItem({
      preview: 'Bounded preview', preview_complete: false,
      message: { role: 'assistant', text: 'Bounded preview' }, content_availability: availability,
      contents: availability === 'preparing'
        ? [{ availability: 'preparing', content_id: 'content', source_revision: 'r1', operation_id: 'operation' }]
        : [],
    })])
    expect(page.canonicalComplete).toBe(true)
    const installed = await install(page)
    expect(installed.state.initialLoadStatus).toBe('ready')
    expect(installed.rendered[0]).toMatchObject({
      previewComplete: false, contentRevision: 'r1', contentAvailability: availability,
      contentUnavailableReason: availability === 'preparing' ? 'content_metadata_pending' : 'content_reference_unavailable',
    })
    expect(installed.messages[0]?.contentRef).toBeUndefined()
  })

  it('keeps a same-revision full body during a bounded refresh while accepting updated semantic metadata', async () => {
    const previous = (await install(await readV2())).messages[0]!
    const incoming = (await install(await readV2([wireItem({
      preview: 'Working note.', preview_complete: false,
      message: { ...message, text: 'Working note.', artifacts: [{ id: 'updated-artifact' }], usage: { input_tokens: 20 } },
    })]))).messages[0]!
    const [merged] = reconcileHistoryMessages([previous], [incoming])
    expect(merged).toMatchObject({
      text: message.text, previewComplete: true, ts: 1000, turnId: 'turn',
      artifacts: [{ id: 'updated-artifact' }], usage: { input_tokens: 20 },
    })
  })

  it('restores reasoning, tool metadata, context and usage on a cold v2 load exactly as v4 does', async () => {
    const enriched: ChatHistoryMessage = {
      ...message, tool_calls: [{ type: 'text', text: answer, presentation: 'answer' }],
      provenance_kind: 'cron', provenance_source_session_key: 'source', provenance_source_tool: 'delegate',
      prompt_annotations: [{ body: 'note' }], model: 'synthetic-model', output_tokens: 30,
      contentPreviewComplete: true, contentRevision: 'r1',
    }
    const v2 = (await install(await readV2([wireItem({ message: enriched })]))).messages[0]!
    const v4 = (await install(await readV4([enriched]))).messages[0]!
    expect(v2).toEqual(v4)
    expect(v2).toMatchObject({ reasoning: { text: 'Measured reasoning' }, turnId: 'turn', usage: { input_tokens: 10 } })
  })

  it('preserves literal JSON answers instead of treating their text/artifacts keys as an envelope', async () => {
    const text = JSON.stringify({ text: 'A literal JSON document', artifacts: ['example'] })
    const installed = await install(await readV2([wireItem({
      preview: text, message: { role: 'assistant', text },
    })]))
    expect(projectAssistantActivity(installed.rendered[0]!, value => value).answerPart?.rawText).toBe(text)
  })

  it('restores error identity from turn outcomes without exposing raw provider details and retains summaries', async () => {
    const page = await readV2([wireItem({
      role: 'error', preview: 'private-provider-detail',
      message: { role: 'error', text: 'private-provider-detail', turn_context: { turn_id: 'turn' } },
    })], {
      turn_outcomes: [{ turn_id: 'turn', status: 'failed', error_class: 'no_provider', retryable: false, outcome: { kind: 'failed', reason: 'no_provider' } }],
      compaction_summaries: [{ id: 'summary', compaction_id: 'compaction', summary_text: 'Synthetic summary' }],
    })
    expect(page.compactionSummaries[0]).toMatchObject({ summaryText: 'Synthetic summary', compactionId: 'compaction' })
    const installed = await install(page)
    const error = installed.messages.find(row => row.role === 'error')!
    expect(error.text).not.toContain('private-provider-detail')
    expect(error).toMatchObject({ terminalNotice: true, errorCode: 'no_provider', turnId: 'turn' })
  })
})
