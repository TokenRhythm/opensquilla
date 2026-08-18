// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import type { PromptAnnotation } from '@/types/promptAnnotations'
import ChatComposer from './ChatComposer.vue'

function annotation(overrides: Partial<PromptAnnotation> = {}): PromptAnnotation {
  return {
    annotationId: 'annotation-1',
    sessionKey: 'session-a',
    sessionId: null,
    sessionEpoch: null,
    documentId: 'document-a',
    documentName: 'page.html',
    revisionId: 'revision-a',
    generation: 1,
    anchorId: 'anchor-a',
    body: 'Make this button clearer.',
    status: 'draft',
    freshness: 'fresh',
    staleReason: null,
    stateRevision: 1,
    tagName: 'button',
    targetStatus: 'ready',
    targetKind: 'button',
    targetText: 'Continue',
    locator: {},
    quote: '<button>',
    sourceExcerpt: '<button>Before</button>',
    sentMessageId: null,
    sentTurnId: null,
    sentOrder: null,
    createdAt: 1,
    updatedAt: 1,
    schemaVersion: 1,
    ...overrides,
  }
}

function composerProps(overrides: Record<string, unknown> = {}) {
  return {
    modelValue: '',
    'onUpdate:modelValue': () => undefined,
    attachments: [],
    promptAnnotations: [annotation()],
    busySendMode: 'queue',
    hasSendContent: true,
    isStreaming: false,
    canStop: false,
    isNewLanding: false,
    placeholder: 'Send a message',
    sendButtonTitle: 'Send',
    runMode: 'safe',
    allowedRunModes: ['safe', 'full'],
    runModeLocked: false,
    runModeLockMessage: '',
    modelRoutingMode: 'off',
    modelRoutingSettingsBusy: false,
    routerVisualEffectsEnabled: true,
    codingModeEnabled: false,
    codingModeSettingsBusy: false,
    voiceBusy: false,
    voiceRecording: false,
    voiceReady: true,
    floating: true,
    ...overrides,
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ChatComposer prompt annotation drafts', () => {
  it('keeps the draft card visible when the floating composer was retracted', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({ collapsed: true }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-composer')?.classList.contains('chat-composer--collapsed'))
      .toBe(false)
    const chip = host.querySelector<HTMLElement>('.chat-prompt-annotation-chip')
    expect(chip?.dataset.freshness).toBeUndefined()
    expect(chip?.getAttribute('role')).toBe('group')
    expect(host.querySelector('[data-testid="composer-prompt-annotations-label"]')?.textContent)
      .toContain('Annotations · 1')
    expect(chip?.textContent).toContain('Button: Continue')
    expect(chip?.textContent).not.toContain('<button>')
    expect(chip?.textContent).toContain('Make this button clearer.')
    expect(chip?.querySelector('.chat-prompt-annotation-chip__rail')).not.toBeNull()
    expect(chip?.querySelector('[data-testid="prompt-annotation-ready-status"]')).toBeNull()
    expect(chip?.textContent).not.toContain('Pending artifact instructions')
    expect(host.querySelector('.chat-prompt-annotations')?.getAttribute('aria-live')).toBe('polite')
    app.unmount()
  })

  it('still retracts an empty composer when there is no annotation draft', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      collapsed: true,
      promptAnnotations: [],
      hasSendContent: false,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-composer')?.classList.contains('chat-composer--collapsed'))
      .toBe(true)
    app.unmount()
  })

  it('preserves floating composer layout while editing, jumping, and deleting chips', async () => {
    const update = vi.fn()
    const discard = vi.fn()
    const jump = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      onUpdatePromptAnnotation: update,
      onDiscardPromptAnnotation: discard,
      onJumpPromptAnnotation: jump,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-composer')?.classList.contains('chat-composer--floating'))
      .toBe(true)
    expect(host.querySelector('.chat-prompt-annotation-chip__target')?.textContent)
      .toContain('Button: Continue')
    host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip__main')?.click()
    expect(jump).toHaveBeenCalledWith('annotation-1')

    host.querySelector<HTMLButtonElement>(
      '.chat-prompt-annotation-chip .attachment-action:not(.attachment-remove)',
    )?.click()
    await nextTick()
    const input = host.querySelector<HTMLInputElement>('.chat-prompt-annotation-chip__editor input')
    expect(input?.value).toBe('Make this button clearer.')
    if (input) {
      input.value = 'Use a stronger call to action.'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    host.querySelector<HTMLFormElement>('.chat-prompt-annotation-chip__editor')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    expect(update).toHaveBeenCalledWith('annotation-1', 'Use a stronger call to action.')

    await nextTick()
    host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip .attachment-remove')
      ?.click()
    expect(discard).toHaveBeenCalledWith('annotation-1')
    app.unmount()
  })

  it('keeps a compatibility stale draft sendable without exposing revision state', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      promptAnnotations: [annotation({ freshness: 'stale', staleReason: 'head-changed' })],
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-prompt-annotation-chip')?.classList.contains('is-stale'))
      .toBe(false)
    expect(host.querySelector('.chat-prompt-annotation-chip__stale')).toBeNull()
    expect(host.textContent).not.toContain('Select this element again')
    app.unmount()
  })

  it.each([
    {
      name: 'plain text',
      promptAnnotations: [] as PromptAnnotation[],
    },
    {
      name: 'an already-ready annotation',
      promptAnnotations: [annotation()],
    },
  ])('blocks $name while another annotation editor is open', async ({ promptAnnotations }) => {
    const send = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const blockedMessage = 'Add or cancel the annotation you are editing before sending.'
    const app = createApp(ChatComposer, composerProps({
      modelValue: 'Keep this message queued.',
      promptAnnotations,
      sendBlockedMessage: blockedMessage,
      onSend: send,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const button = host.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(button?.disabled).toBe(true)
    expect(button?.title).toBe(blockedMessage)
    expect(host.querySelector('#chat-composer-send-status')?.textContent).toBe(blockedMessage)
    button?.click()
    expect(send).not.toHaveBeenCalled()
    app.unmount()
  })
})
