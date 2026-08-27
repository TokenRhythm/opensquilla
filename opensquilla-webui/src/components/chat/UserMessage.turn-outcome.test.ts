// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('UserMessage turn outcome', () => {
  it('shows restart guidance when an interrupted turn has no assistant message', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'user-restart',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: 'Continue the task',
      timeStr: '',
      showHeader: false,
      turnOutcome: {
        turnId: 'turn-restart',
        status: 'abandoned',
        kind: 'interrupted',
        reason: 'process_restart',
      },
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      showTurnOutcome: true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.turn-outcome--process-restart')?.textContent)
      .toContain("This task won't continue automatically")
    app.unmount()
  })

  it('keeps restart guidance visible when the user message contains annotations', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'user-annotated-restart',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: '',
      timeStr: '',
      showHeader: false,
      promptAnnotations: [{
        annotationId: 'annotation-synthetic-restart',
        documentId: 'document-synthetic',
        documentName: 'fixture.html',
        revisionId: 'revision-synthetic',
        generation: 1,
        anchorId: 'anchor-synthetic',
        body: 'Synthetic annotation request.',
        tagName: 'h2',
        locator: {},
        quote: null,
        sourceExcerpt: null,
        sentOrder: 0,
      }],
      turnOutcome: {
        turnId: 'turn-annotated-restart',
        status: 'abandoned',
        kind: 'interrupted',
        reason: 'process_restart',
      },
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      showTurnOutcome: true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.turn-outcome--process-restart')?.textContent)
      .toContain("This task won't continue automatically")
    app.unmount()
  })

  it('shows a provider failure without replacing the authoritative document outcome', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'user-annotated-generic-outcome',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: '',
      timeStr: '',
      showHeader: false,
      promptAnnotations: [{
        annotationId: 'annotation-synthetic-generic',
        documentId: 'document-synthetic',
        documentName: 'fixture.html',
        revisionId: 'revision-synthetic',
        generation: 1,
        anchorId: 'anchor-synthetic',
        body: 'Synthetic annotation request.',
        tagName: 'p',
        locator: {},
        quote: null,
        sourceExcerpt: null,
        sentOrder: 0,
      }],
      turnOutcome: {
        turnId: 'turn-annotated-generic',
        status: 'failed',
        kind: 'failed',
        reason: 'provider_error',
        documentMutationOutcome: {
          version: 1,
          status: 'not_applied',
        },
      },
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      showTurnOutcome: true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.turn-outcome--failed')).not.toBeNull()
    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')
      ?.getAttribute('data-status')).toBe('not_applied')
    app.unmount()
  })

  it('still hides a successful generic outcome when the user message contains annotations', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'user-annotated-success',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: '',
      timeStr: '',
      showHeader: false,
      promptAnnotations: [{
        annotationId: 'annotation-synthetic-success',
        documentId: 'document-synthetic',
        documentName: 'fixture.html',
        revisionId: 'revision-synthetic',
        generation: 1,
        anchorId: 'anchor-synthetic',
        body: 'Synthetic annotation request.',
        tagName: 'span',
        locator: {},
        quote: null,
        sourceExcerpt: null,
        sentOrder: 0,
      }],
      turnOutcome: {
        turnId: 'turn-annotated-success',
        status: 'succeeded',
        kind: 'completed',
      },
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      showTurnOutcome: true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.turn-outcome')).toBeNull()
    app.unmount()
  })
})
