import { describe, expect, it } from 'vitest'
import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallRenderItem,
} from '@/types/chat'
import {
  createdSessionFromToolCall,
  createdSessionsFromMessage,
} from './createdSessions'

function call(overrides: Partial<ChatToolCall> = {}): ChatToolCall {
  return {
    toolId: 'spawn-1',
    name: 'sessions_spawn',
    displayName: 'sessions_spawn',
    inputPreview: '',
    isRunning: false,
    status: 'success',
    isError: false,
    result: JSON.stringify({
      session_key: 'agent:main:subagent:abc12345',
      status: 'queued',
    }),
    resultPreview: '',
    isOpen: false,
    ...overrides,
  }
}

function message(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: '',
    timeStr: '',
    showHeader: false,
    ...overrides,
  }
}

function timeline(...calls: ChatToolCall[]): ChatStreamTimelineItem[] {
  return [{
    type: 'tool-group',
    key: 'spawn-group',
    group: {
      groupId: 'spawn-group',
      operationKey: 'tool.sessions.spawn',
      label: 'sessions_spawn',
      iconName: 'chat',
      calls: calls.map((item, index) => ({
        ...item,
        renderKey: `spawn-${index}`,
      }) as ChatToolCallRenderItem),
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }]
}

describe('created session tool results', () => {
  it('accepts only completed sessions_spawn results with a subagent session key', () => {
    expect(createdSessionFromToolCall(call())).toEqual({
      callId: 'spawn-1',
      sessionKey: 'agent:main:subagent:abc12345',
    })
    expect(createdSessionFromToolCall(call({ name: 'sessions_send' }))).toBeNull()
    expect(createdSessionFromToolCall(call({ isRunning: true, status: '' }))).toBeNull()
    expect(createdSessionFromToolCall(call({ isError: true, status: 'error' }))).toBeNull()
    expect(createdSessionFromToolCall(call({ result: '{broken' }))).toBeNull()
    expect(createdSessionFromToolCall(call({ result: '{}' }))).toBeNull()
    expect(createdSessionFromToolCall(call({
      result: JSON.stringify({ session_key: 'agent:main:webchat:not-a-child' }),
    }))).toBeNull()
  })

  it('keeps multiple creates ordered and deduplicates replayed call ids', () => {
    const first = call()
    const second = call({
      toolId: 'spawn-2',
      result: JSON.stringify({ session_key: 'agent:main:subagent:def67890' }),
    })
    expect(createdSessionsFromMessage(message({
      timelineItems: timeline(first, second),
      toolCalls: [first, second],
    }))).toEqual([
      { callId: 'spawn-1', sessionKey: 'agent:main:subagent:abc12345' },
      { callId: 'spawn-2', sessionKey: 'agent:main:subagent:def67890' },
    ])
  })

  it('restores links from legacy messages that only carry toolCalls', () => {
    expect(createdSessionsFromMessage(message({ toolCalls: [call()] }))).toEqual([
      { callId: 'spawn-1', sessionKey: 'agent:main:subagent:abc12345' },
    ])
  })

  it('accepts a later completed replay when an earlier copy is still pending', () => {
    expect(createdSessionsFromMessage(message({
      timelineItems: timeline(call({ isRunning: true, status: '', result: '' })),
      toolCalls: [call()],
    }))).toEqual([
      { callId: 'spawn-1', sessionKey: 'agent:main:subagent:abc12345' },
    ])
  })
})
