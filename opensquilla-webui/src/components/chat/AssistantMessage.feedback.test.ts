// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import { RpcClient } from '@/lib/rpc'
import type { ChatRenderedMessage } from '@/types/chat'
import AssistantMessage from './AssistantMessage.vue'

const apps: App[] = []
let messageNumber = 0

async function mountMessage(
  overrides: Partial<ChatRenderedMessage> = {},
  sessionKey = 'feedback-session',
  shareMode = false,
) {
  const message: ChatRenderedMessage = {
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: 'A synthetic answer.',
    timeStr: '',
    ts: null,
    showHeader: false,
    messageId: `feedback-message-${++messageNumber}`,
    ...overrides,
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(AssistantMessage, {
    message,
    sessionKey,
    index: 0,
    shareMode,
    shareSelected: false,
    shareMessageId: message.messageId || 'assistant-0',
    renderMarkdown: (text: string) => text,
    fmtTok: String,
    toolCallGroups: () => [],
    isToolGroupOpen: () => false,
    isToolItemOpen: () => false,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
  })
  app.use(i18n)
  app.mount(el)
  apps.push(app)
  await nextTick()
  const votes = Array.from(el.querySelectorAll<HTMLButtonElement>('.msg-action--vote'))
  return { app, el, votes, message }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('AssistantMessage page-local answer ratings', () => {
  it('rates an answer without routing metadata or a feedback service', async () => {
    const rpcCall = vi.spyOn(RpcClient.prototype, 'call')
    const fetchCall = vi.spyOn(globalThis, 'fetch')
    const { votes } = await mountMessage()
    expect(votes).toHaveLength(2)
    expect(votes[0].title).toBe(i18n.global.t('chat.messageFeedback.up'))

    votes[0].click()
    await nextTick()
    expect(votes[0].getAttribute('aria-pressed')).toBe('true')
    votes[1].click()
    await nextTick()
    expect(votes[0].getAttribute('aria-pressed')).toBe('false')
    expect(votes[1].getAttribute('aria-pressed')).toBe('true')
    votes[1].click()
    await nextTick()
    expect(votes[1].getAttribute('aria-pressed')).toBe('false')
    expect(rpcCall).not.toHaveBeenCalled()
    expect(fetchCall).not.toHaveBeenCalled()
  })

  it('keeps ratings on the same message after remount without leaking across sessions', async () => {
    const first = await mountMessage()
    first.votes[0].click()
    await nextTick()
    first.app.unmount()
    apps.splice(apps.indexOf(first.app), 1)

    const remounted = await mountMessage(first.message)
    expect(remounted.votes[0].getAttribute('aria-pressed')).toBe('true')
    const otherSession = await mountMessage(first.message, 'another-feedback-session')
    const otherMessage = await mountMessage()
    expect(otherSession.votes[0].getAttribute('aria-pressed')).toBe('false')
    expect(otherMessage.votes[0].getAttribute('aria-pressed')).toBe('false')
  })

  it('does not offer ratings on unfinished or unidentifiable messages or shared views', async () => {
    expect((await mountMessage({ isStreaming: true })).votes).toHaveLength(0)
    expect((await mountMessage({ messageId: undefined })).votes).toHaveLength(0)
    expect((await mountMessage({}, '')).votes).toHaveLength(0)
    expect((await mountMessage({}, 'feedback-session', true)).votes).toHaveLength(0)
  })
})
