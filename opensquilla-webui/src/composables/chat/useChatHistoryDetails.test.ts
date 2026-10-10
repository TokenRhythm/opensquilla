// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type EffectScope } from 'vue'
import type { ChatMessage } from '@/types/chat'
import { ContentRangeCache, type ContentDetails } from '@/utils/chat/contentRangeCache'
import { mergeLiveOnlyFields } from '@/utils/chat/historyMerge'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatHistoryDetails } from './useChatHistoryDetails'

const session = 'agent:main:webchat:details'
const scopes: EffectScope[] = []
function message(revision = 'r1'): ChatMessage {
  return {
    role: 'assistant', text: 'Final answer', messageId: 'm1', ts: 1,
    previewComplete: true, reasoning: { text: 'preview', seconds: 3 },
    historyPayloadPreview: { detailsTruncated: true }, contentRevision: revision,
    contentRef: { version: 1, sessionKey: session, sessionId: 's1', messageId: 'm1', view: 'display', revision },
    tool_calls: [{ type: 'tool_use', tool_use_id: 'tool', name: 'read_file', input: { path: 'preview' } }],
  }
}
function fixture() {
  const scope = effectScope()
  scopes.push(scope)
  const sessionKey = ref(session)
  const messages = ref<ChatMessage[]>([message()])
  const api = scope.run(() => useChatHistoryDetails({ sessionKey, messages }))!
  const rendered = scope.run(() => useChatRenderedMessages({
    messages, sessionKey, routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
  }))!
  return { scope, api, sessionKey, messages, rendered: rendered.renderedMessages }
}
function details(): ContentDetails {
  return {
    messageId: 'm1', reasoning: '思考🙂'.repeat(5_000),
    toolCalls: [
      { type: 'tool_use', tool_use_id: 'tool', name: 'read_file', input: { path: 'path'.repeat(6_000) } },
      { type: 'tool_result', tool_use_id: 'tool', name: 'read_file', result: 'result'.repeat(5_000) },
      { type: 'text', text: 'Final answer', presentation: 'answer' },
    ],
  }
}
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.restoreAllMocks() })

describe('history details disclosure ownership', () => {
  it('restores long reasoning and tools through the existing renderer without changing the complete answer', async () => {
    const full = details()
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockResolvedValue(full)
    const { api, messages, rendered } = fixture()
    expect(read).not.toHaveBeenCalled()
    await api.setExpanded(messages.value[0]!.contentRef, true)
    expect(messages.value[0]).toMatchObject({ text: 'Final answer', previewComplete: true, detailsCompleteRevision: 'r1' })
    expect(rendered.value[0]?.reasoning?.text).toBe(full.reasoning)
    expect(rendered.value[0]?.toolCalls?.[0]?.inputRaw).toContain('path'.repeat(6_000))
    expect(rendered.value[0]?.toolCalls?.[0]?.result).toBe('result'.repeat(5_000))
    expect(rendered.value[0]?.timelineItems?.map(item => item.type)).toEqual(['tool-group', 'text'])
    await api.setExpanded(messages.value[0]!.contentRef, true)
    expect(read).toHaveBeenCalledOnce()
  })

  it('coalesces overlapping opens and aborts a closed disclosure without late installation', async () => {
    let resolve!: (value: ContentDetails) => void
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockImplementation(() => new Promise(done => { resolve = done }))
    const { api, messages } = fixture()
    const first = api.setExpanded(messages.value[0]!.contentRef, true)
    expect(api.setExpanded(messages.value[0]!.contentRef, true)).toBe(first)
    await api.setExpanded(messages.value[0]!.contentRef, false)
    expect(read.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    resolve(details())
    await first
    expect(messages.value[0]?.reasoning?.text).toBe('preview')
  })

  it.each(['session', 'revision', 'dispose'] as const)('fences a pending read on %s change', async change => {
    let resolve!: (value: ContentDetails) => void
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockImplementation(() => new Promise(done => { resolve = done }))
    const { api, messages, sessionKey, scope } = fixture()
    const request = api.setExpanded(messages.value[0]!.contentRef, true)
    if (change === 'session') sessionKey.value = 'another-session'
    else if (change === 'revision') messages.value = [message('r2')]
    else scope.stop()
    expect(read.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    resolve(details())
    await request
    expect(messages.value[0]?.reasoning?.text).toBe('preview')
  })

  it('keeps existing details on failure and permits a later explicit open to retry', async () => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockRejectedValueOnce(new Error('too large')).mockResolvedValue(details())
    const { api, messages } = fixture()
    await api.setExpanded(messages.value[0]!.contentRef, true)
    expect(messages.value[0]?.reasoning?.text).toBe('preview')
    await api.setExpanded(messages.value[0]!.contentRef, false)
    await api.setExpanded(messages.value[0]!.contentRef, true)
    expect(messages.value[0]?.reasoning?.text).toBe(details().reasoning)
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('retains loaded details only across positively matching storage revisions', async () => {
    vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockResolvedValue(details())
    const { api, messages } = fixture()
    await api.setExpanded(messages.value[0]!.contentRef, true)
    const current = messages.value[0]!
    const same = mergeLiveOnlyFields(current, message())
    expect(same.reasoning?.text).toBe(details().reasoning)
    expect(same.historyPayloadPreview?.detailsTruncated).toBe(false)
    const next = mergeLiveOnlyFields(current, message('r2'))
    expect(next.reasoning?.text).toBe('preview')
    expect(next.detailsCompleteRevision).toBeUndefined()
    for (const change of [{ sessionId: 'another-session' }, { source: 'compacted' as const }]) {
      const elsewhere = message()
      elsewhere.contentRef = { ...elsewhere.contentRef!, ...change }
      const merged = mergeLiveOnlyFields(current, elsewhere)
      expect(merged.reasoning?.text).toBe('preview')
      expect(merged.detailsCompleteRevision).toBeUndefined()
    }
  })

  it('retains an explicit live timeline and its text, tool and interrupt positions', async () => {
    const full = details()
    vi.spyOn(ContentRangeCache.prototype, 'readDetails').mockResolvedValue(full)
    const { api, messages } = fixture()
    const timeline = [
      { type: 'tool-group', groupId: 'group', activityOrder: 1 },
      { type: 'interrupt', approvalId: 'approval', activityOrder: 2 },
      { type: 'text', raw: 'Final answer', presentation: 'answer' as const, activityOrder: 3 },
    ]
    messages.value[0]!.timeline = timeline
    await api.setExpanded(messages.value[0]!.contentRef, true)
    expect(messages.value[0]?.timeline).toEqual(timeline)
  })
})
