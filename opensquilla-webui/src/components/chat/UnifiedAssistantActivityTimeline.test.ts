// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'

import i18n from '@/i18n'
import type { ChatStreamTimelineItem, ChatToolCallRenderItem } from '@/types/chat'
import type { ReasoningBlock } from '@/types/turnlog'
import { projectAssistantActivityTimeline } from '@/utils/chat/assistantActivity'
import UnifiedAssistantActivityTimeline from './UnifiedAssistantActivityTimeline.vue'

const mounted: App[] = []

function tool(id: string, name: string, order: number): ChatToolCallRenderItem {
  return {
    toolId: id,
    renderKey: id,
    name,
    displayName: name,
    inputPreview: '',
    isRunning: false,
    status: 'success',
    isError: false,
    result: '',
    resultPreview: '',
    isOpen: false,
    activityOrder: order,
  }
}

function group(call: ChatToolCallRenderItem): ChatStreamTimelineItem {
  return {
    type: 'tool-group',
    key: `group:${call.toolId}`,
    activityOrder: call.activityOrder,
    group: {
      groupId: `group:${call.toolId}`,
      operationKey: call.name,
      label: call.name,
      iconName: 'gear',
      calls: [call],
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
      activityOrder: call.activityOrder,
    },
  }
}

describe('UnifiedAssistantActivityTimeline', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    i18n.global.locale.value = 'en'
  })
  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
  })

  it('renders the target multi-leg turn in stream order instead of fixed regions', async () => {
    const items: ChatStreamTimelineItem[] = [
      { type: 'text', key: 'text-1', rawText: '第一段', html: '第一段', activityOrder: 31 },
      group(tool('skill-view', 'skill_view', 41)),
      { type: 'text', key: 'text-2', rawText: '第二段', html: '第二段', activityOrder: 140 },
      group(tool('write-file', 'write_file', 154)),
      { type: 'text', key: 'text-3', rawText: '马上发布', html: '马上发布', activityOrder: 3767 },
      group(tool('publish', 'publish_artifact', 3768)),
    ]
    const statusHistory = [
      { action: 'provider:requesting', label: 'Waiting', at: 4_000, activityOrder: 4 },
      { action: 'provider:reasoning', label: 'Reasoning', at: 5_000, activityOrder: 5 },
      { action: 'write:1', label: 'Writing', at: 31_000, activityOrder: 31 },
      { action: 'provider:requesting', label: 'Waiting', at: 50_000, activityOrder: 50 },
      { action: 'provider:reasoning', label: 'Reasoning', at: 51_000, activityOrder: 51 },
      { action: 'write:2', label: 'Writing', at: 140_000, activityOrder: 140 },
      { action: 'provider:requesting', label: 'Waiting', at: 3_744_000, activityOrder: 3744 },
      { action: 'provider:reasoning', label: 'Reasoning', at: 3_746_000, activityOrder: 3746 },
      { action: 'write:3', label: 'Writing', at: 3_767_000, activityOrder: 3767 },
      { action: 'write:4', label: 'Writing', at: 3_805_000, activityOrder: 3805 },
    ]
    const reasoningBlocks: ReasoningBlock[] = [
      { id: 'r1', index: 0, text: '思考一', status: 'completed', startedAt: 6_000, endedAt: 8_000, contentKind: 'reasoning', activityOrder: 6 },
      { id: 'r2', index: 1, text: '思考二', status: 'completed', startedAt: 52_000, endedAt: 54_000, contentKind: 'reasoning', activityOrder: 52 },
      { id: 'r3', index: 2, text: '思考三', status: 'completed', startedAt: 3_747_000, endedAt: 3_766_000, contentKind: 'reasoning', activityOrder: 3747 },
    ]
    const projection = projectAssistantActivityTimeline(items, {
      lifecycle: 'settled',
      statusHistory,
      endedAt: 3_806_000,
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({
      render: () => h(UnifiedAssistantActivityTimeline, {
        projection,
        timelineItems: items,
        reasoningBlocks,
        isToolGroupOpen: () => false,
        isToolItemOpen: () => false,
        toolGroupStatusText: () => '完成',
        toolStatusText: () => '完成',
        toolSecondaryText: () => '',
      }),
    })
    mounted.push(app)
    app.use(i18n)
    app.mount(root)
    await nextTick()

    const entries = [...root.querySelectorAll('[data-activity-entry="true"]')]
    const orders = entries
      .map(node => Number(node.getAttribute('data-activity-order')))
    expect(orders).toEqual([
      4, 6, 31, 31, 41,
      50, 52, 140, 140, 154,
      3744, 3747, 3767, 3767, 3768, 3805,
    ])
    expect(entries.map(node => node.getAttribute('data-activity-id'))).toEqual([
      expect.any(String),
      'r1',
      expect.any(String),
      'text-1',
      'tool:skill-view',
      expect.any(String),
      'r2',
      expect.any(String),
      'text-2',
      'tool:write-file',
      expect.any(String),
      'r3',
      expect.any(String),
      'text-3',
      'tool:publish',
      expect.any(String),
    ])
    const visible = root.textContent || ''
    let cursor = -1
    for (const token of [
      'Model response',
      '思考一',
      '第一段',
      'Used tools',
      'Model response',
      '思考二',
      '第二段',
      'Edited files',
      'Model response',
      '思考三',
      '马上发布',
      'Created artifacts',
      'Answer composition',
    ]) {
      const next = visible.indexOf(token, cursor + 1)
      expect(next, `missing or reordered token: ${token}`).toBeGreaterThan(cursor)
      cursor = next
    }
    expect(root.textContent).toContain('马上发布')
  })

  it('stably projects hundreds of already ordered semantic items', async () => {
    const items: ChatStreamTimelineItem[] = Array.from({ length: 500 }, (_, index) => ({
      type: 'text',
      key: `text-${index}`,
      rawText: `step ${index}`,
      html: `step ${index}`,
      activityOrder: index + 1,
    }))
    const projection = projectAssistantActivityTimeline(items, {
      lifecycle: 'settled',
      statusHistory: [],
      endedAt: 1_000,
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({
      render: () => h(UnifiedAssistantActivityTimeline, {
        projection,
        timelineItems: items,
        reasoningBlocks: [],
        isToolGroupOpen: () => false,
        isToolItemOpen: () => false,
        toolGroupStatusText: () => '',
        toolStatusText: () => '',
        toolSecondaryText: () => '',
      }),
    })
    mounted.push(app)
    app.use(i18n)
    app.mount(root)
    await nextTick()

    const entries = [...root.querySelectorAll('[data-activity-entry="true"]')]
    expect(entries).toHaveLength(500)
    expect(entries.map(node => Number(node.getAttribute('data-activity-order'))))
      .toEqual(Array.from({ length: 500 }, (_, index) => index + 1))
  })
})
