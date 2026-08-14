// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallRenderItem,
} from '@/types/chat'
import AssistantMessage from './AssistantMessage.vue'

const apps: App[] = []

function spawnCall(overrides: Partial<ChatToolCall> = {}): ChatToolCall {
  return {
    toolId: 'spawn-1',
    name: 'sessions_spawn',
    displayName: 'sessions_spawn',
    inputPreview: '',
    isRunning: false,
    status: 'success',
    isError: false,
    result: JSON.stringify({ session_key: 'agent:main:subagent:abc12345' }),
    resultPreview: '',
    isOpen: false,
    ...overrides,
  }
}

function timeline(call: ChatToolCall): ChatStreamTimelineItem[] {
  return [{
    type: 'tool-group',
    key: 'spawn-group',
    group: {
      groupId: 'spawn-group',
      operationKey: 'tool.sessions.spawn',
      label: 'sessions_spawn',
      iconName: 'chat',
      calls: [{ ...call, renderKey: 'spawn-render' } as ChatToolCallRenderItem],
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }]
}

async function mount(message: ChatRenderedMessage, onOpenSession = vi.fn()) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(AssistantMessage, {
    message,
    index: 0,
    shareMode: false,
    shareSelected: false,
    shareMessageId: 'assistant-0',
    renderMarkdown: (text: string) => text,
    fmtTok: (value: number) => String(value),
    toolCallGroups: (calls: ChatToolCall[]) => timeline(calls[0]!).map(item => (
      item.type === 'tool-group' ? item.group : null
    )).filter(Boolean),
    isToolGroupOpen: () => true,
    isToolItemOpen: () => true,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
    onOpenSession,
  })
  apps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { el, onOpenSession }
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

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('AssistantMessage created session cards', () => {
  it('replaces a successful live spawn tool result with an openable card', async () => {
    const call = spawnCall()
    const { el, onOpenSession } = await mount(message({
      toolCalls: [call],
      timelineItems: timeline(call),
    }))

    expect(el.querySelectorAll('[data-testid="session-created-card"]')).toHaveLength(1)
    expect(el.textContent).toContain('Chat created')
    expect(el.textContent).not.toContain('session_key')
    expect(el.querySelector('.tool-row')).toBeNull()

    el.querySelector<HTMLButtonElement>('.session-created-card__open')?.click()
    await nextTick()
    expect(onOpenSession).toHaveBeenCalledWith('agent:main:subagent:abc12345')
  })

  it('renders ordered cards from restored tool calls without a timeline', async () => {
    const first = spawnCall()
    const second = spawnCall({
      toolId: 'spawn-2',
      result: JSON.stringify({ session_key: 'agent:main:subagent:def67890' }),
    })
    const { el } = await mount(message({ toolCalls: [first, second] }))

    expect(Array.from(el.querySelectorAll('[data-session-key]')).map(node => (
      node.getAttribute('data-session-key')
    ))).toEqual([
      'agent:main:subagent:abc12345',
      'agent:main:subagent:def67890',
    ])
  })

  it('keeps malformed and failed results out of the semantic card surface', async () => {
    const { el } = await mount(message({
      toolCalls: [
        spawnCall({ result: '{}' }),
        spawnCall({ toolId: 'spawn-2', status: 'error', isError: true }),
      ],
    }))
    expect(el.querySelector('[data-testid="session-created-card"]')).toBeNull()
  })

  it('rehomes the card below the parent final reply without restoring raw tool output', async () => {
    const call = spawnCall()
    const source = await mount(message({
      toolCalls: [call],
      timelineItems: timeline(call),
      createdSessionLinks: [],
    }))
    expect(source.el.querySelector('[data-testid="session-created-card"]')).toBeNull()
    expect(source.el.querySelector('.tool-row')).toBeNull()

    const target = await mount(message({
      text: 'Parent final reply',
      createdSessionLinks: [{
        callId: 'spawn-1',
        sessionKey: 'agent:main:subagent:abc12345',
      }],
    }))
    const replyText = target.el.textContent || ''
    expect(replyText.indexOf('Parent final reply')).toBeLessThan(replyText.indexOf('Chat created'))
  })
})
