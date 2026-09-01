// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { SESSION_CONVERSATION_KEY } from '@/modules/sessionConversation'
import { sessionConversationTestDouble } from '@/testing/sessionConversation.test-helper'
import type { ChatMessageMeta, ChatRenderedMessage } from '@/types/chat'

const submitRouteFeedback = vi.fn()
const sessionConversation = sessionConversationTestDouble({ submitRouteFeedback })

import i18n from '@/i18n'
import AssistantMessage from './AssistantMessage.vue'

function meta(overrides: Partial<ChatMessageMeta> = {}): ChatMessageMeta {
  return {
    model: 'z-ai/glm-5.2',
    modelShort: 'glm-5.2',
    input: 10,
    output: 20,
    hasTokens: true,
    cachedTokens: 0,
    reasoningTokens: 0,
    costUsd: 0.001,
    hasSaved: false,
    savedLabel: '',
    ...overrides,
  }
}

function assistantMessage(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: 'answer',
    timeStr: '',
    ts: null,
    showHeader: false,
    ...overrides,
  }
}

async function mountMessage(message: ChatRenderedMessage) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(AssistantMessage, {
    message,
    index: 0,
    shareMode: false,
    shareSelected: false,
    shareMessageId: message.messageId || 'assistant-0',
    renderMarkdown: (text: string) => text,
    fmtTok: (value: number) => String(value),
    toolCallGroups: () => [],
    isToolGroupOpen: () => false,
    isToolItemOpen: () => false,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
  })
  app.use(i18n)
  app.provide(SESSION_CONVERSATION_KEY, sessionConversation)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  submitRouteFeedback.mockReset()
  submitRouteFeedback.mockResolvedValue({ accepted: true })
  document.body.innerHTML = ''
})

describe('AssistantMessage routing feedback buttons', () => {
  it('renders no thumbs without a decision id', async () => {
    const { app, el } = await mountMessage(assistantMessage({ meta: meta() }))
    expect(el.querySelectorAll('.msg-action--vote')).toHaveLength(0)
    app.unmount()
  })

  it('renders thumbs and submits when a decision id is present', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({ meta: meta({ decisionId: 'dec-ui-1' }) }),
    )
    const votes = el.querySelectorAll<HTMLButtonElement>('.msg-action--vote')
    expect(votes).toHaveLength(2)

    votes[1].click()
    await nextTick()
    expect(submitRouteFeedback).toHaveBeenCalledWith('dec-ui-1', 'down')
    app.unmount()
  })

  it('uses ensemble-specific tooltips on ensemble turns', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({
        meta: meta({
          decisionId: 'dec-ui-2',
          ensemble: {
            profile: 'default',
            modelCount: 3,
            totalCandidates: 3,
            requestCount: 3,
            fallbackUsed: false,
            fallbackReason: '',
            costUsd: 0.002,
            savedUsd: 0,
            savedPct: 0,
            models: [],
          },
        }),
      }),
    )
    const [up] = Array.from(el.querySelectorAll<HTMLButtonElement>('.msg-action--vote'))
    expect(up.title).toBe(i18n.global.t('chat.routeFeedback.upEnsemble'))
    app.unmount()
  })
})
