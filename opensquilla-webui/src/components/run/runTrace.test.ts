import { describe, expect, it } from 'vitest'

import type { ChatStreamTimelineItem } from '@/types/chat'
import type { SessionReadMessage } from '@/modules/sessionReadLifecycle'
import {
  nodeStepsFromHistoryMessage,
  nodeStepsFromSessionReadMessage,
  nodeStepsFromTimeline,
} from './runTrace'

describe('run trace tool identity', () => {
  it('preserves the original tool name when flattening chat timeline items', () => {
    const items: ChatStreamTimelineItem[] = [{
      type: 'tool-group',
      key: 'web-fetch-group',
      group: {
        groupId: 'web-fetch-group',
        operationKey: 'web.read',
        label: 'Read web page',
        iconName: 'monitor',
        calls: [{
          toolId: 'fetch-1',
          renderKey: 'fetch-1',
          name: 'web_fetch',
          displayName: 'web_fetch',
          inputPreview: '',
          isRunning: false,
          status: 'success',
          isError: false,
          result: '2026 results',
          resultPreview: '2026 results',
          isOpen: false,
        }],
        secondary: '',
        isRunning: false,
        isError: false,
        status: 'success',
      },
    }]

    expect(nodeStepsFromTimeline(items)[0]).toMatchObject({
      toolName: 'web_fetch',
      operationKey: 'web.read',
    })
  })

  it('preserves the original tool name when flattening persisted history', () => {
    const message = {
      tool_calls: [{
        tool_use_id: 'search-1',
        name: 'MCPURLSearch',
        result: 'Found 7 results.',
      }],
    } satisfies Parameters<typeof nodeStepsFromHistoryMessage>[0]

    expect(nodeStepsFromHistoryMessage(message)[0]).toMatchObject({
      toolName: 'MCPURLSearch',
      operationKey: 'tool.mcpurlsearch',
    })
  })

  it('reads tool calls from the domain session history projection', () => {
    const message = {
      toolCalls: [{
        tool_use_id: 'search-2',
        name: 'MCPURLSearch',
        result: 'Found 9 results.',
      }],
    } as Pick<SessionReadMessage, 'toolCalls'>

    expect(nodeStepsFromSessionReadMessage(message)[0]).toMatchObject({
      toolName: 'MCPURLSearch',
      operationKey: 'tool.mcpurlsearch',
      output: 'Found 9 results.',
    })
  })
})
