import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage } from '@/types/chat'
import { useChatStream } from './useChatStream'

function makeStream() {
  const messages = ref<ChatMessage[]>([])
  const stream = useChatStream({
    messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false),
    applySessionRunState: vi.fn(), renderMarkdown: text => text,
    stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text,
    scrollToBottom: vi.fn(),
  })
  return { stream, messages }
}

describe('continuous live body', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('keeps appending beyond the former 1 MiB boundary, including Unicode and missing Done text', () => {
    const { stream, messages } = makeStream()
    const prefix = 'a'.repeat(1024 * 1024)
    const tail = '继续🙂答复。'.repeat(1024)
    stream.appendDelta(prefix)
    stream.appendDelta(tail)
    expect(stream.foldedTurn.value.rawText).toBe(prefix + tail)
    expect(stream.streamPreviewComplete.value).toBe(true)
    stream.reconcileFinalText(undefined)
    stream.endStreaming()
    expect(messages.value[0]?.text).toBe(prefix + tail)
    expect(messages.value[0]?.previewComplete).not.toBe(false)
    stream.cleanup()
  })

  it('keeps late old-call text above a steer and continues the new answer', () => {
    const { stream, messages } = makeStream()
    const prefix = 'a'.repeat(1024 * 1024)
    stream.appendDelta(prefix, 'answer', { modelCallId: 'call-1', iteration: 1 })
    stream.checkpointForUserMessage('turn-1', 'steer-1')
    messages.value.push({ role: 'user', text: 'continue', ts: null, clientId: 'steer-1', turnId: 'turn-1' })
    stream.acknowledgeSteerBoundary('steer-1', 'call-2', 2)
    stream.appendDelta('旧段尾🙂', 'answer', { modelCallId: 'call-1', iteration: 1 })
    stream.appendDelta('新段正文', 'answer', { modelCallId: 'call-2', iteration: 2 })
    stream.endStreaming()
    expect(messages.value.map(message => message.role)).toEqual(['assistant', 'user', 'assistant'])
    expect(messages.value.filter(message => message.role === 'assistant').map(message => message.text))
      .toEqual([prefix + '旧段尾🙂', '新段正文'])
    stream.cleanup()
  })

  it.each(['terminal', 'reset'] as const)('accepts a complete long %s snapshot without clipping', (source) => {
    const { stream, messages } = makeStream()
    const body = '中文🙂'.repeat(128 * 1024)
    stream.startStreaming()
    if (source === 'terminal') stream.reconcileFinalText(body)
    else stream.resetAnswerGeneration({ textSnapshot: body })
    stream.endStreaming()
    expect(messages.value[0]?.text).toBe(body)
    expect(messages.value[0]?.previewComplete).not.toBe(false)
    stream.cleanup()
  })

  it('keeps a known replay gap incomplete without hiding subsequent live text or accepting a short receipt as full', () => {
    const { stream, messages } = makeStream()
    stream.appendDelta('stored summary')
    stream.markTextPreviewIncomplete()
    stream.appendDelta(' + latest live answer')
    stream.reconcileFinalText('short receipt')
    stream.endStreaming({ reason: 'aborted' })
    expect(messages.value[0]).toMatchObject({
      text: 'stored summary + latest live answer', previewComplete: false,
      contentAvailability: 'preparing', interrupted: true,
    })
    stream.cleanup()
  })

  it.each(['generation', 'router'] as const)('replaces a discarded replay gap on %s reset', (reset) => {
    const { stream } = makeStream()
    stream.appendDelta('old summary')
    stream.markTextPreviewIncomplete()
    if (reset === 'generation') stream.resetAnswerGeneration({ textSnapshot: 'replacement' })
    else {
      stream.resetStreamForRouterReplay()
      stream.appendDelta('replacement')
    }
    expect(stream.foldedTurn.value.rawText).toBe('replacement')
    expect(stream.streamPreviewComplete.value).toBe(true)
    stream.cleanup()
  })

  it.each(['generation', 'router'] as const)('retains a checkpoint replay gap across %s reset without suppressing the replacement', (reset) => {
    const { stream } = makeStream()
    stream.appendDelta('stored summary')
    stream.markTextPreviewIncomplete()
    stream.checkpointForUserMessage('turn-1', 'steer-1')
    stream.acknowledgeSteerBoundary('steer-1')
    if (reset === 'generation') stream.resetAnswerGeneration({ textSnapshot: 'replacement' })
    else {
      stream.resetStreamForRouterReplay()
      stream.appendDelta('replacement')
    }
    expect(stream.foldedTurn.value.rawText).toBe('replacement')
    expect(stream.streamPreviewComplete.value).toBe(false)
    stream.cleanup()
  })

  it.each([false, true])('honors explicit empty/suppressed=%s while preserving completed tools', (suppressed) => {
    const { stream, messages } = makeStream()
    stream.appendDelta('stale summary')
    stream.markTextPreviewIncomplete()
    stream.appendToolCall({ id: 'tool-1', name: 'exec_command' })
    stream.appendToolResult({ id: 'tool-1', name: 'exec_command', result: 'kept' })
    if (!suppressed) stream.reconcileFinalText('')
    stream.endStreaming({ suppressed })
    expect(messages.value[0]?.text).toBe('')
    expect(messages.value[0]?.previewComplete).not.toBe(false)
    expect(messages.value[0]?.tool_calls?.[0]?.id).toBe('tool-1')
    stream.cleanup()
  })

  it('clears replay completeness state on session reset', () => {
    const { stream, messages } = makeStream()
    stream.appendDelta('old summary')
    stream.markTextPreviewIncomplete()
    stream.resetLiveTurnState()
    stream.appendDelta('new session')
    stream.endStreaming()
    expect(messages.value[0]?.text).toBe('new session')
    expect(messages.value[0]?.previewComplete).not.toBe(false)
    stream.cleanup()
  })
})
