import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/types/chat'
import { interleaveHistoryModelCallSegments } from './historyModelCallSegments'

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
})
