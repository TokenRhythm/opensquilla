// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import AssistantMessage from './AssistantMessage.vue'
import source from './AssistantMessage.vue?raw'

function assistantMessage(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: 'fused answer',
    timeStr: '',
    ts: null,
    showHeader: false,
    ...overrides,
  }
}

async function mountMessage(message: ChatRenderedMessage, propOverrides: Record<string, unknown> = {}) {
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
    ...propOverrides,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('AssistantMessage ensemble footer metadata', () => {
  it('marks only cron-provenance assistant rows as scheduled', async () => {
    const { app, el } = await mountMessage(assistantMessage({
      provenanceKind: 'cron',
      provenanceSourceTool: 'cron.run',
    }))

    const badge = el.querySelector<HTMLElement>('.msg-provenance-chip')
    expect(badge?.textContent).toContain('Scheduled')
    expect(badge?.title).toContain('cron.run')
    app.unmount()

    const regular = await mountMessage(assistantMessage({ provenanceKind: 'internal_system' }))
    expect(regular.el.querySelector('.msg-provenance-chip')).toBeNull()
    regular.app.unmount()

    const unsafe = await mountMessage(assistantMessage({
      provenanceKind: 'cron',
      provenanceSourceTool: 'cron.run\nBearer secret',
    }))
    expect(unsafe.el.querySelector<HTMLElement>('.msg-provenance-chip')?.title).not.toContain('Bearer')
    unsafe.app.unmount()
  })

  it('shows the current message token counts in its usage popover', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({
        meta: {
          model: 'z-ai/glm-5.2-20260616',
          modelShort: 'glm-5.2-20260616',
          input: 120,
          output: 40,
          hasTokens: true,
          cachedTokens: 0,
          reasoningTokens: 0,
          costUsd: 0.050328,
          hasSaved: false,
          savedLabel: '',
        },
      }),
    )

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')?.click()
    await nextTick()

    expect(el.querySelector('.msg-meta-popover__label')?.textContent).toBe('tokens')
    expect(el.querySelector('.msg-meta-popover__value')?.textContent).toBe('↑120 ↓40')
    app.unmount()
  })

  it('does not present ensemble aggregate metadata as single-model footer metadata', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({
        meta: {
          model: 'z-ai/glm-5.2-20260616',
          modelShort: 'glm-5.2-20260616',
          input: 120,
          output: 40,
          hasTokens: true,
          cachedTokens: 0,
          reasoningTokens: 0,
          costUsd: 0.371989,
          hasSaved: true,
          savedLabel: 'Saved ~92%',
          ensemble: {
            profile: 'default',
            modelCount: 5,
            totalCandidates: 5,
            requestCount: 5,
            fallbackUsed: false,
            fallbackReason: '',
            costUsd: 0.371989,
            savedUsd: 0,
            savedPct: 0,
            models: [],
          },
        },
      }),
    )

    expect(el.querySelector('.msg-meta__model')).toBeNull()
    expect(el.querySelector('.msg-meta__cost')).toBeNull()
    expect(el.querySelector('.savings-indicator')).toBeNull()
    expect(el.querySelector('.msg-meta__ensemble')?.textContent).toBe('Ensemble · 5 models')
    app.unmount()
  })

  it('never renders a savings row in the ensemble usage popover', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({
        meta: {
          model: 'z-ai/glm-5.2-20260616',
          modelShort: 'glm-5.2-20260616',
          input: 2700000,
          output: 39500,
          hasTokens: true,
          cachedTokens: 0,
          reasoningTokens: 0,
          costUsd: 3.590973,
          hasSaved: false,
          savedLabel: '',
          ensemble: {
            profile: 'router_dynamic/c1',
            modelCount: 3,
            totalCandidates: 3,
            requestCount: 36,
            fallbackUsed: false,
            fallbackReason: '',
            costUsd: 3.590973,
            // Stale nonzero savings persisted by older gateways must not
            // resurface a savings row when the session is restored.
            savedUsd: 2.725456,
            savedPct: 69,
            models: [],
          },
        },
      }),
    )

    el.querySelector<HTMLElement>('.msg-meta__more-btn')?.click()
    await nextTick()

    const labels = Array.from(el.querySelectorAll('.msg-meta-popover__label')).map(
      node => node.textContent,
    )
    expect(labels).toContain('cost')
    expect(labels).not.toContain('saved')
    app.unmount()
  })

  it('keeps the savings badge for non-ensemble optimized messages', async () => {
    const { app, el } = await mountMessage(
      assistantMessage({
        meta: {
          model: 'z-ai/glm-5.2-20260616',
          modelShort: 'glm-5.2-20260616',
          input: 120,
          output: 40,
          hasTokens: true,
          cachedTokens: 0,
          reasoningTokens: 0,
          costUsd: 0.050328,
          hasSaved: true,
          savedLabel: 'Saved ~92%',
        },
      }),
    )

    expect(el.querySelector('.savings-indicator')?.textContent).toBe('Saved ~92%')
    app.unmount()
  })

  it('keeps the ensemble summary broad enough on compact layouts', () => {
    expect(source).not.toContain('max-width: 7rem;')
    expect(source).toContain('max-width: min(14rem, 100%);')
  })

  it('does not toggle share selection for stopped-output notices', async () => {
    const onToggleShare = vi.fn()
    const { app, el } = await mountMessage(
      assistantMessage({
        text: 'Stopped after 1s',
        messageId: 'client-stop-notice:task-1',
        stopNotice: true,
      }),
      {
        shareMode: true,
        shareMessageId: 'client-stop-notice:task-1',
        onToggleShare,
      },
    )

    el.querySelector<HTMLElement>('.msg-ai')?.click()
    await nextTick()

    expect(el.querySelector('.chat-share-picker')).toBeNull()
    expect(onToggleShare).not.toHaveBeenCalled()
    app.unmount()
  })
})
