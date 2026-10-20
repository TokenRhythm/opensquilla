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
  it.each([
    { format: 'docx' as const, kind: 'text' as const, paragraphIds: ['paragraph-1'], truncated: false },
    { format: 'pptx' as const, kind: 'element' as const, slideIndex: 0, shapeId: 'title', truncated: false },
    { format: 'xlsx' as const, kind: 'range' as const, sheetIndex: 0, range: 'B2:C4', truncated: false },
  ])('opens the $format draft target through the shared locate action', async selection => {
    const jump = vi.fn()
    const send = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      promptAnnotations: [annotation({ selection })],
      onJumpPromptAnnotation: jump,
      onSend: send,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()
    const main = host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip__main')!
    expect(main.disabled).toBe(false)
    main.click()
    expect(jump).toHaveBeenCalledWith('annotation-1')
    expect(send).not.toHaveBeenCalled()
    expect(host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip .attachment-action')?.disabled).toBe(false)
    app.unmount()
  })

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

  it('shows a stale draft warning while retaining locate, edit and delete recovery actions', async () => {
    const jump = vi.fn()
    const discard = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      promptAnnotations: [annotation({ freshness: 'stale', staleReason: 'head-changed' })],
      onJumpPromptAnnotation: jump,
      onDiscardPromptAnnotation: discard,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-prompt-annotation-chip')?.classList.contains('is-stale'))
      .toBe(true)
    expect(host.querySelector('.chat-prompt-annotation-chip__stale')?.textContent?.trim()).toBeTruthy()
    const main = host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip__main')!
    expect(main.disabled).toBe(false)
    main.click()
    expect(jump).toHaveBeenCalledWith('annotation-1')
    expect(host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip .attachment-action:not(.attachment-remove)')?.disabled)
      .toBe(false)
    host.querySelector<HTMLButtonElement>('.chat-prompt-annotation-chip .attachment-remove')!.click()
    expect(discard).toHaveBeenCalledWith('annotation-1')
    app.unmount()
  })

  it('keeps an Office stale warning visible and blocks sending when its owner reports a stale selection', async () => {
    const send = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      promptAnnotations: [annotation({ freshness: 'stale', selection: {
        format: 'pptx', kind: 'element', slideIndex: 0, shapeId: 'title', truncated: false,
      } })],
      sendBlockedMessage: 'Select the target again before sending.',
      onSend: send,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()
    expect(host.querySelector('.chat-prompt-annotation-chip__stale')?.textContent?.trim()).toBeTruthy()
    const sendButton = host.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(sendButton.disabled).toBe(true)
    sendButton.click()
    expect(send).not.toHaveBeenCalled()
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
    expect(button?.hasAttribute('title')).toBe(false)
    expect(button?.getAttribute('aria-describedby')).toBeNull()
    expect(host.querySelector('.chat-composer-send-status')).toBeNull()
    expect(host.querySelector('.chat-composer-status-announcement')).toBeNull()
    expect(host.querySelector('.chat-send-tooltip')).toBeNull()
    expect(host.innerHTML).not.toContain(blockedMessage)
    button?.click()
    expect(send).not.toHaveBeenCalled()
    app.unmount()
  })
})
