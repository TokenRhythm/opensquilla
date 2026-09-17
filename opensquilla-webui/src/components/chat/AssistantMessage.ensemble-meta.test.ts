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

  it('keeps model, cost, and token details behind one info control', async () => {
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

    expect(el.querySelectorAll('.msg-meta__more-btn')).toHaveLength(1)
    expect(el.querySelector('.msg-meta__model')).toBeNull()
    expect(el.querySelector('.msg-meta__cost')).toBeNull()
    const rows = Array.from(el.querySelectorAll('.msg-meta-popover__row')).map(row => row.textContent)
    expect(rows).toContain('modelglm-5.2-20260616')
    expect(rows).toContain('cost$0.050328')
    expect(rows).toContain('tokens↑120 ↓40')
    app.unmount()
  })

  it('renders numeric ensemble costs without completeness warnings or dashes', async () => {
    const { app, el } = await mountMessage(assistantMessage({
      meta: {
        model: 'free/ensemble',
        modelShort: 'ensemble',
        input: 20,
        output: 4,
        hasTokens: true,
        cachedTokens: 0,
        reasoningTokens: 0,
        costUsd: 0.123,
        hasSaved: false,
        savedLabel: '',
        usageUnknown: true,
        coverageStatus: 'usage_unknown',
        ensemble: {
          profile: 'free',
          modelCount: 2,
          totalCandidates: 2,
          requestCount: 2,
          fallbackUsed: false,
          fallbackReason: '',
          costUsd: 0.123,
          savedUsd: 0,
          savedPct: 0,
          models: [
            {
              role: 'proposer',
              label: 'proposer',
              provider: 'free',
              model: 'free/proposer',
              modelShort: 'proposer',
              input: 10,
              output: 2,
              costUsd: 0,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'unknown',
              model: 'unknown/aggregator',
              modelShort: 'aggregator',
              input: 10,
              output: 2,
              costUsd: 0.123,
            },
          ],
        },
      },
    }))

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')?.click()
    await nextTick()

    const rows = Array.from(el.querySelectorAll('.msg-meta-popover__row')).map(row => row.textContent)
    expect(rows).toContain('cost$0.123')
    expect(Array.from(el.querySelectorAll('.msg-meta-popover__model-cost')).map(node => node.textContent?.trim())).toEqual([
      '$0',
      '$0.123',
    ])
    expect(el.textContent).not.toContain('—')
    expect(el.querySelector('[data-turn-usage-coverage="incomplete"]')).toBeNull()
    app.unmount()
  })

  it('supports hover, pinning, Escape, focus exit, and outside dismissal', async () => {
    const { app, el } = await mountMessage(assistantMessage({
      meta: {
        model: 'test/model',
        modelShort: 'model',
        input: 120,
        output: 40,
        hasTokens: true,
        cachedTokens: 12,
        reasoningTokens: 8,
        costUsd: 0.001,
        hasSaved: false,
        savedLabel: '',
      },
    }))
    const root = el.querySelector<HTMLElement>('.msg-meta__more')!
    const trigger = el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')!

    root.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()
    expect(el.querySelector('.msg-meta-popover')).not.toBeNull()
    root.dispatchEvent(new MouseEvent('mouseleave'))
    await nextTick()
    expect(el.querySelector('.msg-meta-popover')).toBeNull()

    trigger.click()
    await nextTick()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(el.querySelector('.msg-meta-popover')).toBeNull()
    expect(document.activeElement).toBe(trigger)

    trigger.click()
    await nextTick()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await nextTick()
    expect(el.querySelector('.msg-meta-popover')).toBeNull()

    trigger.click()
    await nextTick()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    root.dispatchEvent(new FocusEvent('focusout', { relatedTarget: outside, bubbles: true }))
    await nextTick()
    expect(el.querySelector('.msg-meta-popover')).toBeNull()
    app.unmount()
  })

  it('keeps ensemble metadata behind the same single info control', async () => {
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
    expect(el.querySelector('.msg-meta__ensemble')).toBeNull()
    expect(el.querySelectorAll('.msg-meta__more-btn')).toHaveLength(1)
    app.unmount()
  })

  it('uses only Proposer and Aggregator names in the ensemble model list', async () => {
    const internalRoles = ['primary', 'contrast', 'fast_check', 'critic', 'aggregator']
    const { app, el } = await mountMessage(assistantMessage({
      meta: {
        model: 'tokenrhythm/model-5',
        modelShort: 'model-5',
        input: 100,
        output: 20,
        hasTokens: true,
        cachedTokens: 0,
        reasoningTokens: 0,
        costUsd: 0.05,
        hasSaved: false,
        savedLabel: '',
        ensemble: {
          profile: 'custom_b5',
          modelCount: 4,
          totalCandidates: 4,
          requestCount: 5,
          fallbackUsed: false,
          fallbackReason: '',
          costUsd: 0.05,
          savedUsd: 0,
          savedPct: 0,
          models: internalRoles.map((role, index) => ({
            role,
            label: role,
            provider: 'tokenrhythm',
            model: `model-${index + 1}`,
            modelShort: `model-${index + 1}`,
            input: 10,
            output: 2,
            costUsd: 0.01,
          })),
        },
      },
    }))

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')?.click()
    await nextTick()

    const displayedRoles = Array.from(
      el.querySelectorAll<HTMLElement>('.msg-meta-popover__model-role'),
      node => node.textContent?.trim(),
    )
    expect(displayedRoles).toEqual([
      'Proposer ·',
      'Proposer ·',
      'Proposer ·',
      'Proposer ·',
      'Aggregator ·',
    ])
    expect(el.textContent).not.toContain('primary')
    expect(el.textContent).not.toContain('contrast')
    expect(el.textContent).not.toContain('fast check')
    expect(el.textContent).not.toContain('critic')
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

  it('does not show a savings badge beside the info control', async () => {
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

    expect(el.querySelector('.savings-indicator')).toBeNull()
    expect(el.querySelectorAll('.msg-meta__more-btn')).toHaveLength(1)
    app.unmount()
  })

  it('does not render inline model, cost, ensemble, or savings metadata', () => {
    expect(source).not.toContain('class="msg-meta__model"')
    expect(source).not.toContain('class="msg-meta__cost"')
    expect(source).not.toContain('class="msg-meta__ensemble"')
    expect(source).not.toContain('class="savings-indicator"')
  })

  it('opens the info popover inward from the left edge of the message pane', () => {
    const popoverRule = source.match(/\.msg-meta-popover\s*\{([^}]*)\}/)?.[1] || ''

    expect(popoverRule).toMatch(/\bleft:\s*0;/)
    expect(popoverRule).not.toContain('translateX(-50%)')
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
