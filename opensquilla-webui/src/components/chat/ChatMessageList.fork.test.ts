// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createPinia } from 'pinia'
import { createApp, nextTick, type App } from 'vue'

import i18n from '@/i18n'
import type { ChatRenderedMessage, DisplayAttachment } from '@/types/chat'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []

function user(id: string, turnKey: string, turnId?: string): ChatRenderedMessage {
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
    turnId,
  }
}

function usageBarrierError(
  id: string,
  turnId: string,
  userMessageId?: string,
): ChatRenderedMessage {
  return {
    id,
    messageId: id,
    turnId,
    role: 'error',
    displayRole: 'error',
    roleLabel: 'Error',
    text: 'Usage accounting temporarily unavailable.',
    timeStr: '',
    showHeader: true,
    errorCode: 'usage_accounting_busy',
    turnOutcome: {
      turnId,
      status: 'failed',
      usageCallIndex: 1,
      noPriorProviderDispatch: true,
      replaySafe: true,
      ...(userMessageId ? { userMessageId } : {}),
    },
  }
}

function usageBarrierAssistant(
  id: string,
  turnId: string,
  userMessageId: string,
  usageCallIndex: number,
): ChatRenderedMessage {
  return {
    id,
    messageId: id,
    turnId,
    turnKey: `turn:${turnId}`,
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: '',
    timeStr: '',
    showHeader: false,
    parts: [],
    statusHistory: [],
    turnOutcome: {
      turnId,
      status: 'failed',
      errorClass: 'usage_accounting_busy',
      usageCallIndex,
      noPriorProviderDispatch: usageCallIndex === 1,
      replaySafe: usageCallIndex === 1,
      userMessageId,
    },
  }
}

function displayAttachment(kind: DisplayAttachment['kind']): DisplayAttachment {
  return {
    kind,
    displayId: `history:${kind}`,
    renderKey: `history:${kind}`,
    name: `${kind}.txt`,
    mime: 'text/plain',
    ...(kind === 'inline' ? { downloadData: 'cmVxdWVzdA==' } : {}),
    ...(kind === 'staged' ? { sha256_ref: 'a'.repeat(64) } : {}),
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

describe('ChatMessageList restart outcome anchor', () => {
  it('renders restart guidance once on the final visible message in the turn', () => {
    const source = user('user-restart', 'turn:restart', 'turn-restart')
    const partial = assistant('assistant-restart', 'turn:restart', 'turn-restart')
    const outcome = {
      turnId: 'turn-restart',
      status: 'abandoned',
      kind: 'interrupted',
      reason: 'process_restart',
    }
    source.turnOutcome = outcome
    partial.turnOutcome = outcome

    const { host } = mountList([source, partial])
    const notices = host.querySelectorAll('.turn-outcome--process-restart')

    expect(notices).toHaveLength(1)
    expect(notices[0]?.closest('.msg-ai')).not.toBeNull()
  })
})

describe('ChatMessageList usage barrier retry anchor', () => {
  it('shows Retry when the durable same-turn user is loaded', () => {
    const { host } = mountList([
      user('user-safe', 'turn:safe', 'turn-safe'),
      user('user-steer', 'turn:safe-steer', 'turn-safe'),
      usageBarrierError('error-safe', 'turn-safe', 'user-safe'),
    ])

    expect(host.querySelector('.msg-error-card__resume')?.textContent).toContain('Retry')
  })

  it('hides Retry when pagination only retained a previous-turn user', () => {
    const { host } = mountList([
      user('user-old', 'turn:old', 'turn-old'),
      usageBarrierError('error-new', 'turn-new', 'user-new'),
    ])

    expect(host.querySelector('.msg-error-card__resume')).toBeNull()
  })

  it.each([
    ['missing', undefined],
    ['wrong', 'user-steer'],
  ])('hides Retry when the primary-user identity is %s', (_label, userMessageId) => {
    const { host } = mountList([
      user('user-primary', 'turn:safe', 'turn-safe'),
      usageBarrierError('error-safe', 'turn-safe', userMessageId),
    ])

    expect(host.querySelector('.msg-error-card__resume')).toBeNull()
  })

  it.each(['inline', 'staged', 'file'] as const)(
    'hides Retry when the primary request has a %s display attachment',
    (kind) => {
      const primary = user('user-primary', 'turn:safe', 'turn-safe')
      primary.attachments = [displayAttachment(kind)]
      const { host } = mountList([
        primary,
        usageBarrierError('error-safe', 'turn-safe', 'user-primary'),
      ])

      expect(host.querySelector('.msg-error-card__resume')).toBeNull()
    },
  )
})

describe('ChatMessageList assistant usage barrier regenerate', () => {
  it('hides Regenerate for an unsafe status-only barrier with a same-turn steer', () => {
    const { host } = mountList([
      user('user-primary', 'turn:primary', 'turn-safe'),
      user('user-steer', 'turn:steer', 'turn-safe'),
      usageBarrierAssistant('assistant-status', 'turn-safe', 'user-primary', 2),
    ])

    expect(host.querySelector('[aria-label="Regenerate"]')).toBeNull()
  })

  it('keeps Regenerate for a safe status-only barrier anchored to the primary user', () => {
    const { host } = mountList([
      user('user-primary', 'turn:primary', 'turn-safe'),
      user('user-steer', 'turn:steer', 'turn-safe'),
      usageBarrierAssistant('assistant-status', 'turn-safe', 'user-primary', 1),
    ])

    expect(host.querySelector('[aria-label="Regenerate"]')).not.toBeNull()
  })

  it('hides Regenerate when a safe status-only barrier points to a request with attachments', () => {
    const primary = user('user-primary', 'turn:primary', 'turn-safe')
    primary.attachments = [displayAttachment('inline')]
    const { host } = mountList([
      primary,
      user('user-steer', 'turn:steer', 'turn-safe'),
      usageBarrierAssistant('assistant-status', 'turn-safe', 'user-primary', 1),
    ])

    expect(host.querySelector('[aria-label="Regenerate"]')).toBeNull()
  })
})
