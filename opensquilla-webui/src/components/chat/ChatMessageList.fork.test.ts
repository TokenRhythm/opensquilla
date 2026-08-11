// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createPinia } from 'pinia'
import { createApp, nextTick, type App } from 'vue'

import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []

function user(id: string, turnKey: string): ChatRenderedMessage {
  return {
    id,
    messageId: id,
    turnKey,
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: `Question ${id}`,
    timeStr: '',
    showHeader: false,
  }
}

function assistant(
  id: string,
  turnKey: string,
  turnId?: string,
): ChatRenderedMessage {
  return {
    id,
    messageId: id,
    turnKey,
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: `Answer ${id}`,
    timeStr: '',
    showHeader: false,
    parts: [],
    statusHistory: [],
    ...(turnId
      ? { turnOutcome: { turnId, status: 'completed', kind: 'completed' } }
      : {}),
  }
}

function mountList(
  messages: ChatRenderedMessage[],
  options: { isStreaming?: boolean } = {},
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const forks: Array<string | undefined> = []
  const app = createApp(ChatMessageList, {
    messages,
    sessionKey: 'agent:main:webchat:parent',
    shareMode: false,
    selectedMessageIds: new Set<string>(),
    stripTimePrefix: (value: string) => value,
    renderMarkdown: (value: string) => value,
    fmtTok: (value: number) => String(value),
    subagentSummary: (value: string) => value,
    subagentBody: (value: string) => value,
    toolCallGroups: () => [],
    isToolGroupOpen: () => false,
    isToolItemOpen: () => false,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
    isStreaming: options.isStreaming ?? false,
    onForkConversation: (throughTurnId?: string) => forks.push(throughTurnId),
  })
  app.use(i18n)
  app.use(createPinia())
  app.mount(host)
  apps.push(app)
  return { host, forks }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatMessageList fork targets', () => {
  it('offers every durable completed assistant turn tip and emits its inclusive turn id', async () => {
    const { host, forks } = mountList([
      user('user-old', 'turn:old'),
      assistant('assistant-old', 'turn:old', 'turn-old'),
      user('user-new', 'turn:new'),
      assistant('assistant-new', 'turn:new', 'turn-new'),
    ])

    const buttons = [...host.querySelectorAll<HTMLButtonElement>('[data-testid="fork-conversation"]')]
    expect(buttons).toHaveLength(2)
    buttons[0].click()
    buttons[1].click()
    await nextTick()

    expect(forks).toEqual(['turn-old', 'turn-new'])
  })

  it('keeps the full-fork fallback only on the legacy conversation tip', async () => {
    const { host, forks } = mountList([
      user('user-old', 'turn:legacy-old'),
      assistant('assistant-old', 'turn:legacy-old'),
      user('user-tip', 'turn:legacy-tip'),
      assistant('assistant-tip', 'turn:legacy-tip'),
    ])

    const buttons = [...host.querySelectorAll<HTMLButtonElement>('[data-testid="fork-conversation"]')]
    expect(buttons).toHaveLength(1)
    buttons[0].click()
    await nextTick()

    expect(forks).toEqual([undefined])
  })

  it('does not expose fork actions while a turn is streaming', () => {
    const { host } = mountList([
      user('user-tip', 'turn:tip'),
      assistant('assistant-tip', 'turn:tip', 'turn-tip'),
    ], { isStreaming: true })

    expect(host.querySelector('[data-testid="fork-conversation"]')).toBeNull()
  })
})
