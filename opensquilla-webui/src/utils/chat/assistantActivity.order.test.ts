import { describe, expect, it } from 'vitest'
import type { ChatStreamTimelineItem, ChatToolCallRenderItem } from '@/types/chat'
import type { ReasoningBlock } from '@/types/turnlog'
import { canMergeAssistantActivityOrder, type AssistantActivityStatusStep } from './assistantActivity'

const reasoning = (activityOrder?: number, text = 'Thinking'): ReasoningBlock => ({
  id: 'thought', index: 0, text, status: 'completed', startedAt: 0,
  contentKind: 'reasoning', activityOrder,
})
const textItem = (activityOrder?: number): ChatStreamTimelineItem => ({
  type: 'text', key: 'narration', rawText: 'Progress', html: 'Progress', activityOrder,
})
const tools = (orders: Array<number | undefined>, activityOrder?: number): ChatStreamTimelineItem => ({
  type: 'tool-group', key: 'group', activityOrder,
  group: {
    groupId: 'group', operationKey: 'command.run', label: 'Run', iconName: 'gear',
    calls: orders.map((order, index): ChatToolCallRenderItem => ({
      toolId: String(index), renderKey: String(index), name: 'shell', displayName: 'Run',
      inputRaw: '{}', inputPreview: '', isRunning: false, status: 'success',
      isError: false, result: 'ok', resultPreview: 'ok', isOpen: false, activityOrder: order,
    })),
    secondary: '', isRunning: false, isError: false, status: 'success',
  },
})

describe('canMergeAssistantActivityOrder', () => {
  it('accepts real per-call positions even without a group position', () => {
    expect(canMergeAssistantActivityOrder([], [reasoning(2)], [tools([1, 3])])).toBe(true)
  })
  it('accepts stable equal positions and valid group fallback', () => {
    expect(canMergeAssistantActivityOrder([], [reasoning(2)], [tools([undefined, 2], 2)])).toBe(true)
  })
  it('rejects reversed call or narration positions instead of rendering an empty unified view', () => {
    expect(canMergeAssistantActivityOrder([], [reasoning(2)], [tools([3, 1])])).toBe(false)
    expect(canMergeAssistantActivityOrder([], [], [textItem(4), textItem(3)])).toBe(false)
    expect(canMergeAssistantActivityOrder([], [reasoning(4), reasoning(2)], [])).toBe(false)
  })
  it('requires order for every visible source and does not turn invalid positions into guesses', () => {
    expect(canMergeAssistantActivityOrder([], [reasoning()], [tools([1])])).toBe(false)
    expect(canMergeAssistantActivityOrder([], [reasoning(1)], [textItem()])).toBe(false)
    expect(canMergeAssistantActivityOrder([], [], [tools([0], 2)])).toBe(false)
    expect(canMergeAssistantActivityOrder([], [], [])).toBe(false)
  })
  it('retains an unattributed aggregate failure in the compatibility renderer', () => {
    const item = tools([1, 3])
    if (item.type !== 'tool-group') throw new Error('Expected tool group')
    item.group.isError = true
    item.group.status = 'error'
    expect(canMergeAssistantActivityOrder([], [reasoning(2)], [item])).toBe(false)
    expect(item.group.calls.every(call => !call.isError && call.status === 'success')).toBe(true)
  })
  it('ignores empty reasoning and hidden historical phases without weakening live ordering', () => {
    const hidden: AssistantActivityStatusStep = {
      key: 'status', label: { code: 'chat.activity.provider.reasoning', params: {} },
      at: 1, isCurrent: false,
    }
    expect(canMergeAssistantActivityOrder([hidden], [reasoning(undefined, '')], [tools([1])])).toBe(true)
    const historical: AssistantActivityStatusStep = {
      ...hidden, label: { code: 'chat.activity.provider.waiting', params: {} },
    }
    expect(canMergeAssistantActivityOrder([historical], [], [tools([1])])).toBe(true)
    const visible = { ...historical, isCurrent: true }
    expect(canMergeAssistantActivityOrder([visible], [], [tools([1])])).toBe(false)
    const retry: AssistantActivityStatusStep = {
      ...hidden, label: { code: 'chat.activity.provider.retrying', params: { attempt: 2, limit: 3 } },
    }
    expect(canMergeAssistantActivityOrder([retry], [], [tools([1])])).toBe(false)
  })
})
