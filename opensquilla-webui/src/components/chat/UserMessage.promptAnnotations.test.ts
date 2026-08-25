// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import type { WorkbenchResource } from '@/types/workbenchResources'
import UserMessage from './UserMessage.vue'

afterEach(() => {
  document.body.innerHTML = ''
})

function annotationMessage(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'message-1',
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: '',
    timeStr: '',
    showHeader: false,
    promptAnnotations: [{
      annotationId: 'annotation-1',
      documentId: 'document-1',
      documentName: 'page.html',
      revisionId: 'revision-1',
      generation: 1,
      anchorId: 'anchor-1',
      body: 'Make the heading concise.',
      tagName: 'h1',
      targetKind: 'heading',
      targetText: 'Welcome',
      locator: {},
      quote: '<h1>',
      sourceExcerpt: null,
      sentOrder: 0,
    }],
    ...overrides,
  }
}

async function mountAnnotationMessage(
  message: ChatRenderedMessage,
  attachmentResources?: ReadonlyMap<string, WorkbenchResource>,
) {
  const host = document.createElement('div')
  document.body.append(host)
  const reuse = vi.fn()
  const previewAttachment = vi.fn()
  const editAttachment = vi.fn()
  const downloadAttachment = vi.fn(async () => true)
  const defaultAttachmentResources = new Map<string, WorkbenchResource>(
    (message.attachments || []).flatMap(attachment => attachment.attachmentId
      ? [[attachment.attachmentId, {
          resource: { type: 'attachment' as const, id: attachment.attachmentId },
          name: attachment.name,
          mime: attachment.mime,
          size: attachment.size,
          capabilities: {
            preview: true,
            download: true,
            selectionContext: false,
            manualEdit: true,
            agentEdit: false,
            edit: true,
            publish: false,
          },
          relations: {},
        } satisfies WorkbenchResource] as const]
      : []),
  )
  const app = createApp(UserMessage, {
    message,
    shareMode: false,
    shareSelected: false,
    shareMessageId: message.id,
    stripTimePrefix: (value: string) => value,
    copyMessage: async () => true,
    downloadAttachment,
    workbenchResourcePreviewEnabled: true,
    workbenchResourceEditEnabled: true,
    workbenchAttachmentResources: attachmentResources || defaultAttachmentResources,
    canReusePromptAnnotations: true,
    onReusePromptAnnotation: reuse,
    onPreviewAttachment: previewAttachment,
    onEditAttachment: editAttachment,
  })
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { app, downloadAttachment, editAttachment, host, previewAttachment, reuse }
}

describe('UserMessage prompt annotation snapshots', () => {
  it('opens an HTML attachment from its card and keeps original download separate', async () => {
    const attachment = {
      kind: 'staged' as const,
      displayId: 'attachment-1',
      renderKey: 'attachment-1',
      attachmentId: 'att_opaque_1',
      name: 'uploaded.html',
      mime: 'text/html',
      size: 42,
      download_url: '/api/v1/attachments/fixture',
    }
    const {
      app,
      downloadAttachment,
      editAttachment,
      host,
      previewAttachment,
    } = await mountAnnotationMessage(
      annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
    )

    const open = host.querySelector<HTMLButtonElement>(
      '[aria-label="Open uploaded.html"]',
    )
    const download = host.querySelector<HTMLButtonElement>(
      '[aria-label="Download uploaded.html"]',
    )
    expect(open).not.toBeNull()
    expect(download).not.toBeNull()
    expect(host.querySelector(
      '.msg-file-resource__actions [aria-label^="Edit"]',
    )).toBeNull()
    open?.click()
    download?.click()
    expect(previewAttachment).toHaveBeenCalledWith(attachment)
    expect(downloadAttachment).toHaveBeenCalledWith(attachment)
    expect(editAttachment).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps read-only preview available when document import is unavailable', async () => {
    const attachment = {
      kind: 'staged' as const,
      displayId: 'attachment-preview-only',
      renderKey: 'attachment-preview-only',
      attachmentId: 'att_opaque_preview_only',
      name: 'preview-only.html',
      mime: 'text/html',
      size: 42,
      download_url: '/api/v1/attachments/fixture',
    }
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(UserMessage, {
      message: annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
      shareMode: false,
      shareSelected: false,
      shareMessageId: 'preview-only-message',
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      workbenchResourcePreviewEnabled: true,
      workbenchResourceEditEnabled: false,
      workbenchAttachmentResources: new Map([[
        attachment.attachmentId,
        {
          resource: { type: 'attachment', id: attachment.attachmentId },
          name: attachment.name,
          mime: attachment.mime,
          size: attachment.size,
          capabilities: {
            preview: true,
            download: true,
            selectionContext: false,
            manualEdit: false,
            agentEdit: false,
            edit: false,
            publish: false,
          },
          relations: {},
        } satisfies WorkbenchResource,
      ]]),
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('[aria-label="Open preview-only.html"]')).not.toBeNull()
    expect(host.querySelector('[aria-label="Download preview-only.html"]')).not.toBeNull()
    expect(host.querySelector('[aria-label="Edit a copy of preview-only.html"]')).toBeNull()
    app.unmount()
  })

  it('hides both attachment actions when an older Gateway exposes neither RPC', async () => {
    const attachment = {
      kind: 'staged' as const,
      displayId: 'attachment-old-gateway',
      renderKey: 'attachment-old-gateway',
      attachmentId: 'att_old_gateway',
      name: 'legacy.html',
      mime: 'text/html',
      size: 42,
      download_url: '/api/v1/attachments/legacy',
    }
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(UserMessage, {
      message: annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
      shareMode: false,
      shareSelected: false,
      shareMessageId: 'old-gateway-message',
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      workbenchResourcePreviewEnabled: false,
      workbenchResourceEditEnabled: false,
      workbenchAttachmentResources: new Map([[
        attachment.attachmentId,
        {
          resource: { type: 'attachment', id: attachment.attachmentId },
          name: attachment.name,
          mime: attachment.mime,
          size: attachment.size,
          capabilities: {
            preview: true,
            download: true,
            selectionContext: false,
            manualEdit: true,
            agentEdit: false,
            edit: true,
            publish: false,
          },
          relations: {},
        } satisfies WorkbenchResource,
      ]]),
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('[aria-label="Open legacy.html"]')).toBeNull()
    expect(host.querySelector('[aria-label="Download legacy.html"]')).not.toBeNull()
    expect(host.querySelector('[aria-label="Edit a copy of legacy.html"]')).toBeNull()
    app.unmount()
  })

  it('disables invalid HTML actions from the authoritative resource capability', async () => {
    const attachment = {
      kind: 'staged' as const,
      displayId: 'attachment-invalid',
      renderKey: 'attachment-invalid',
      attachmentId: 'att_invalid_utf8',
      name: 'invalid.html',
      mime: 'text/html',
      size: 42,
      download_url: '/api/v1/attachments/invalid',
    }
    const resource = {
      resource: { type: 'attachment' as const, id: attachment.attachmentId },
      name: attachment.name,
      mime: attachment.mime,
      size: attachment.size,
      capabilities: {
        preview: false,
        download: true,
        selectionContext: false,
        manualEdit: false,
        agentEdit: false,
        edit: false,
        publish: false,
        previewReasonCode: 'html_encoding_unsupported',
        editReasonCode: 'html_encoding_unsupported',
      },
      relations: {},
    } satisfies WorkbenchResource
    const { app, editAttachment, host, previewAttachment } = await mountAnnotationMessage(
      annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
      new Map([[attachment.attachmentId, resource]]),
    )

    const download = host.querySelector<HTMLButtonElement>(
      '[aria-label="Download invalid.html"]',
    )
    expect(download).not.toBeNull()
    expect(host.querySelector('[aria-label^="Open invalid.html"]')).toBeNull()
    expect(host.querySelector('[aria-label^="Edit a copy of invalid.html"]')).toBeNull()
    const reason = host.querySelector('[data-testid="attachment-workbench-unavailable"]')
    expect(reason?.textContent).toContain('not valid UTF-8')
    expect(reason?.textContent).not.toContain('html_encoding_unsupported')
    download?.click()
    expect(previewAttachment).not.toHaveBeenCalled()
    expect(editAttachment).not.toHaveBeenCalled()
    app.unmount()
  })

  it('opens an oversized editable HTML resource in its available preview', async () => {
    const attachment = {
      kind: 'staged' as const,
      displayId: 'attachment-oversized',
      renderKey: 'attachment-oversized',
      attachmentId: 'att_oversized_html',
      name: 'large.html',
      mime: 'text/html',
      size: 3 * 1024 * 1024,
      download_url: '/api/v1/attachments/large',
    }
    const resource = {
      resource: { type: 'attachment' as const, id: attachment.attachmentId },
      name: attachment.name,
      mime: attachment.mime,
      size: attachment.size,
      capabilities: {
        preview: true,
        download: true,
        selectionContext: false,
        manualEdit: false,
        agentEdit: false,
        edit: false,
        publish: false,
        editReasonCode: 'html_edit_size_unsupported',
      },
      relations: {},
    } satisfies WorkbenchResource
    const { app, editAttachment, host, previewAttachment } = await mountAnnotationMessage(
      annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
      new Map([[attachment.attachmentId, resource]]),
    )

    const open = host.querySelector<HTMLButtonElement>(
      '[aria-label="Open large.html"]',
    )
    expect(open?.disabled).toBe(false)
    expect(host.querySelector('[aria-label^="Edit a copy of large.html"]')).toBeNull()
    open?.click()
    expect(previewAttachment).toHaveBeenCalledWith(attachment)
    expect(editAttachment).not.toHaveBeenCalled()
    app.unmount()
  })

  it('renders immutable annotation cards even when the user message has no text', async () => {
    const message = annotationMessage()
    const { app, host, reuse } = await mountAnnotationMessage(message)

    const card = host.querySelector('[data-testid="sent-prompt-annotation"]')
    expect(card?.textContent).toContain('page.html')
    expect(card?.textContent).toContain('Heading: Welcome')
    expect(card?.textContent).not.toContain('<h1>')
    expect(card?.textContent).toContain('Make the heading concise.')
    expect(card?.querySelector('.msg-prompt-annotation__rail')).not.toBeNull()
    expect(card?.querySelector('code')).toBeNull()
    expect(host.querySelector('[data-testid="sent-prompt-annotations-label"]')?.textContent)
      .toContain('Annotations · 1')
    expect(host.querySelector('.msg-user-bubble')).toBeNull()
    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')).toBeNull()
    const copyButton = host.querySelector<HTMLButtonElement>('.msg-prompt-annotation__reuse')
    expect(copyButton?.textContent).not.toContain('Copy as new annotation')
    expect(copyButton?.getAttribute('aria-label')).toBe(
      'Copy the modification request and choose its target on the page.',
    )
    expect(copyButton?.getAttribute('title')).toBe(
      'Copy the modification request and choose its target on the page.',
    )
    copyButton?.click()
    expect(reuse).toHaveBeenCalledWith(message.promptAnnotations?.[0])
    app.unmount()
  })

  it.each([
    {
      name: 'renders an applied page update',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'succeeded',
          documentMutationOutcome: {
            version: 1,
            status: 'applied',
            changeSetId: 'change-set-1',
            resultRevisionId: 'revision-2',
          },
        },
      }),
      expected: 'applied',
    },
    {
      name: 'renders a page update failure',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'succeeded',
          documentMutationOutcome: { version: 1, status: 'not_applied' },
        },
      }),
      expected: 'not_applied',
    },
    {
      name: 'maps an internal conflict to the same page update failure',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'succeeded',
          documentMutationOutcome: { version: 1, status: 'conflict' },
        },
      }),
      expected: 'not_applied',
    },
    {
      name: 'renders a pending page update check',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'failed',
          documentMutationOutcome: { version: 1, status: 'ambiguous' },
        },
      }),
      expected: 'ambiguous',
    },
  ])('$name', async ({ message, expected }) => {
    const { app, host } = await mountAnnotationMessage(message)

    const status = host.querySelector('[data-testid="prompt-annotation-turn-status"]')
    expect(status?.getAttribute('data-status')).toBe(expected)
    expect(status?.textContent).toBe({
      applied: 'Page updated',
      not_applied: 'Couldn’t update page',
      ambiguous: 'Checking update',
    }[expected])
    expect(status?.textContent).not.toMatch(/receipt|reconciliation|revision|conflict/i)

    app.unmount()
  })

  it.each([
    {
      name: 'Gateway acceptance',
      message: annotationMessage({ messageId: 'user-1', turnId: 'turn-1' }),
    },
    {
      name: 'a terminal turn without a mutation receipt',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: { turnId: 'turn-1', status: 'succeeded' },
      }),
    },
  ])('does not render a standalone status for $name', async ({ message }) => {
    const { app, host } = await mountAnnotationMessage(message)
    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')).toBeNull()
    app.unmount()
  })

  it('maps a corrected proposal to the same page-updated state', async () => {
    const { app, host } = await mountAnnotationMessage(annotationMessage({
      messageId: 'user-1',
      turnId: 'turn-1',
      turnOutcome: {
        turnId: 'turn-1',
        status: 'succeeded',
        documentMutationOutcome: {
          version: 1,
          status: 'applied',
          corrected: true,
          proposalAttempts: 2,
        },
      },
    }))

    const card = host.querySelector('[data-testid="prompt-annotation-turn-status"]')
    expect(card?.getAttribute('data-status')).toBe('applied')
    expect(card?.textContent).toBe('Page updated')
    app.unmount()
  })

  it('maps a not-attempted internal outcome to the page update failure state', async () => {
    const { app, host } = await mountAnnotationMessage(annotationMessage({
      messageId: 'user-1',
      turnId: 'turn-1',
      turnOutcome: {
        turnId: 'turn-1',
        status: 'succeeded',
        documentMutationOutcome: { version: 1, status: 'not_attempted' },
      },
    }))

    const card = host.querySelector('[data-testid="prompt-annotation-turn-status"]')
    expect(card?.getAttribute('data-status')).toBe('not_applied')
    expect(card?.textContent).toBe('Couldn’t update page')
    app.unmount()
  })
})
