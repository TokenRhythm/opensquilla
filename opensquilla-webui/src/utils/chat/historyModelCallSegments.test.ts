import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/types/chat'
import { interleaveHistoryModelCallSegments, interleaveHydratedHistoryModelCallSegments } from './historyModelCallSegments'

function baseTurn(): ChatMessage[] {
  return [{
    role: 'user',
    text: '原始问题',
    ts: 1,
    messageId: 'user-original',
    turnId: 'turn-1',
    restoredFromHistory: true,
  }]
}

function appliedSteer(
  messageId: string,
  text: string,
  modelCallId: string,
  iteration: number,
): ChatMessage {
  return {
    role: 'user',
    text,
    ts: messageId,
    messageId,
    turnId: 'turn-1',
    inputDisposition: 'applied',
    steerModelCallId: modelCallId,
    steerAppliedIteration: iteration,
    restoredFromHistory: true,
  }
}

describe('interleaveHistoryModelCallSegments', () => {
  it('splits assistant output by Unicode codepoint offsets around an applied steer', () => {
    const messages: ChatMessage[] = [
      ...baseTurn(),
      appliedSteer('steer-1', '请补充细节', '2.0', 2),
      {
        role: 'assistant',
        text: '前😀后续',
        ts: 3,
        messageId: 'assistant-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
        usage: {
          routed_tier: 'c1',
          routed_model: 'provider/initial',
          routing_source: 'squilla_router',
          router_model_call_id: '1.0',
          router_iteration: 1,
          model_call_segments: [{
            model_call_id: '2.0',
            iteration: 2,
            start_codepoint: 2,
            end_codepoint: 4,
          }],
        },
      },
    ]

    const result = interleaveHistoryModelCallSegments(messages)

    expect(result.map(message => [message.role, message.text])).toEqual([
      ['user', '原始问题'],
      ['assistant', '前😀'],
      ['user', '请补充细节'],
      ['assistant', '后续'],
    ])
    expect(result[1]?.messageId).toBeUndefined()
    expect(result[1]?.clientId).toContain('history-model-call-segment:')
    expect(result[1]?.usage).toBeUndefined()
    expect(result[1]?.routerUsage).toMatchObject({
      routed_tier: 'c1',
      router_model_call_id: '1.0',
    })
    expect(result[1]?.routerModelCallId).toBe('1.0')
    expect(result[1]?.routerIteration).toBe(1)
    expect(result[3]?.messageId).toBe('assistant-1')
    expect(result[3]?.usage?.model_call_segments).toHaveLength(1)
  })

  it('keeps FIFO steer batches and interleaves multiple continuation calls', () => {
    const messages: ChatMessage[] = [
      ...baseTurn(),
      appliedSteer('steer-a', 'A', '2.0', 2),
      appliedSteer('steer-b', 'B', '2.0', 2),
      appliedSteer('steer-c', 'C', '3.0', 3),
      {
        role: 'assistant',
        text: '前甲乙后',
        ts: 5,
        messageId: 'assistant-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
        usage: {
          model_call_segments: [
            {
              model_call_id: '2.0',
              iteration: 2,
              start_codepoint: 1,
              end_codepoint: 2,
            },
            {
              model_call_id: '3.0',
              iteration: 3,
              start_codepoint: 2,
              end_codepoint: 4,
            },
          ],
        },
      },
    ]

    const result = interleaveHistoryModelCallSegments(messages)

    expect(result.map(message => message.text)).toEqual([
      '原始问题',
      '前',
      'A',
      'B',
      '甲',
      'C',
      '乙后',
    ])
    expect(result.filter(message => message.messageId === 'assistant-1')).toHaveLength(1)
  })

  it('fails closed for invalid UTF-16-like ranges', () => {
    const messages: ChatMessage[] = [
      ...baseTurn(),
      appliedSteer('steer-1', '调整', '2.0', 2),
      {
        role: 'assistant',
        text: '前😀后续',
        ts: 3,
        messageId: 'assistant-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
        usage: {
          model_call_segments: [{
            model_call_id: '2.0',
            iteration: 2,
            start_codepoint: 3,
            end_codepoint: 5,
          }],
        },
      },
    ]

    expect(interleaveHistoryModelCallSegments(messages)).toEqual(messages)
  })

  it('does not reorder an unrelated durable row inside the aggregate block', () => {
    const messages: ChatMessage[] = [
      ...baseTurn(),
      appliedSteer('steer-1', '调整', '2.0', 2),
      {
        role: 'system',
        text: 'durable notice',
        ts: 2,
        messageId: 'notice-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
      },
      {
        role: 'assistant',
        text: '前后',
        ts: 3,
        messageId: 'assistant-1',
        turnId: 'turn-1',
        restoredFromHistory: true,
        usage: {
          model_call_segments: [{
            model_call_id: '2.0',
            iteration: 2,
            start_codepoint: 1,
            end_codepoint: 2,
          }],
        },
      },
    ]

    expect(interleaveHistoryModelCallSegments(messages)).toEqual(messages)
  })

  it('carries one shared content reference and visible slices across transformed rows', () => {
    const contentRef = {
      version: 1 as const,
      sessionKey: 'agent:main:webchat:bounded-history',
      sessionId: 'session-1',
      messageId: 'assistant-1',
      view: 'display' as const,
      byteLength: 20 * 1024,
    }
    const messages: ChatMessage[] = [
      ...baseTurn(),
      appliedSteer('steer-1', '调整', '2.0', 2),
      {
        role: 'assistant',
        text: '前😀后续',
        ts: 3,
        messageId: 'assistant-1',
        turnId: 'turn-1',
        contentRef,
        restoredFromHistory: true,
        usage: {
          model_call_segments: [{
            model_call_id: '2.0',
            iteration: 2,
            start_codepoint: 2,
            end_codepoint: 4,
          }],
        },
      },
    ]

    const result = interleaveHistoryModelCallSegments(messages)
    expect(result[1]).toMatchObject({
      clientId: expect.stringContaining('history-model-call-segment:'),
      contentRef,
      contentSlice: { startCodepoint: 0, endCodepoint: 2 },
    })
    expect(result[3]).toMatchObject({
      messageId: 'assistant-1',
      contentRef,
      contentSlice: { startCodepoint: 2, endCodepoint: 4 },
    })
  })
})

describe('hydrated history segmentation', () => {
  const body = '前😀中🙂后🚀'
  function boundedTurn(): ChatMessage[] {
    return [...baseTurn(), appliedSteer('steer-1', '调整一', '2.0', 2), appliedSteer('steer-2', '调整二', '3.0', 3), {
      role: 'assistant', text: '前', ts: 5, messageId: 'answer', turnId: 'turn-1', previewComplete: false,
      contentRef: { version: 1, sessionKey: 'session-key', sessionId: 'session', messageId: 'answer', view: 'display',
        source: 'active', revision: 'r1', byteLength: 32768, sha256: 'digest' }, contentRevision: 'r1',
      usage: { model_call_segments: [
        { model_call_id: '2.0', iteration: 2, start_codepoint: 2, end_codepoint: 4 },
        { model_call_id: '3.0', iteration: 3, start_codepoint: 4, end_codepoint: 6 },
      ] },
      tool_calls: [{ type: 'tool_use', tool_use_id: 'call', name: 'read_file', input: {} }],
      timeline: [{ type: 'tool', toolId: 'call' }],
    }]
  }

  it('retains bounded previews, all empty continuation slices, and one canonical activity owner', () => {
    const rows = boundedTurn()
    const source = rows[rows.length - 1]!
    const result = interleaveHydratedHistoryModelCallSegments(rows, source, body)
    expect(result.map(row => row.text)).toEqual(['原始问题', '前', '调整一', '', '调整二', ''])
    expect(result.filter(row => row.role === 'assistant').map(row => row.contentSlice)).toEqual([
      { startCodepoint: 0, endCodepoint: 2 }, { startCodepoint: 2, endCodepoint: 4 }, { startCodepoint: 4, endCodepoint: 6 },
    ])
    expect(result[result.length - 1]).toMatchObject({ messageId: 'answer', tool_calls: source.tool_calls, timeline: source.timeline, usage: source.usage })
    expect(result.filter(row => row.tool_calls)).toHaveLength(1)
    expect(interleaveHydratedHistoryModelCallSegments(result, source, body)).toBe(result)
    expect(interleaveHistoryModelCallSegments(result)).toBe(result)
  })

  it.each(['sessionKey', 'sessionId', 'messageId', 'source', 'view', 'revision', 'byteLength', 'sha256'] as const)(
    'rejects a delayed body with changed contentRef.%s', field => {
      const rows = boundedTurn()
      const source = rows[rows.length - 1]!
      const stale = { ...source, contentRef: { ...source.contentRef!, [field]: field === 'byteLength' ? 100 : 'different' } }
      expect(interleaveHydratedHistoryModelCallSegments(rows, stale as ChatMessage, body)).toBe(rows)
    },
  )

  it('rejects content revisions, invalid full-body boundaries, and unrelated durable rows', () => {
    const rows = boundedTurn()
    const source = rows[rows.length - 1]!
    expect(interleaveHydratedHistoryModelCallSegments(rows, { ...source, contentRevision: 'r2' }, body)).toBe(rows)
    expect(interleaveHydratedHistoryModelCallSegments(rows, source, body + 'extra')).toBe(rows)
    expect(interleaveHydratedHistoryModelCallSegments(rows, source, 'different')).toBe(rows)
    rows.splice(2, 0, { role: 'system', text: 'notice', ts: 2, messageId: 'notice' })
    expect(interleaveHydratedHistoryModelCallSegments(rows, source, body)).toBe(rows)
  })

  it.each(['turnRunKind', 'provenanceKind', 'planRevisions'] as const)('leaves Plan-owned content intact (%s)', field => {
    const rows = boundedTurn()
    const source = rows[rows.length - 1]!
    Object.assign(source, { [field]: field === 'planRevisions' ? [{ revisionId: 'plan' }] : 'plan_implementation' })
    expect(interleaveHydratedHistoryModelCallSegments(rows, source, body)).toBe(rows)
  })
})
