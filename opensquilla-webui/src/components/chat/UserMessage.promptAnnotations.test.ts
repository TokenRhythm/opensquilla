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
    downloadAttachment: async () => true,
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
  return { app, editAttachment, host, previewAttachment, reuse }
}

describe('UserMessage prompt annotation snapshots', () => {
  it('offers HTML attachment preview and edit only with a stable attachment identity', async () => {
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
    const { app, editAttachment, host, previewAttachment } = await mountAnnotationMessage(
      annotationMessage({ promptAnnotations: [], attachments: [attachment] }),
    )

    const preview = host.querySelector<HTMLButtonElement>(
      '[aria-label="Preview uploaded.html"]',
    )
    const edit = host.querySelector<HTMLButtonElement>(
      '[aria-label="Edit a copy of uploaded.html"]',
    )
    expect(preview).not.toBeNull()
    expect(edit).not.toBeNull()
    preview?.click()
    edit?.click()
    expect(previewAttachment).toHaveBeenCalledWith(attachment)
    expect(editAttachment).toHaveBeenCalledWith(attachment)
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

    expect(host.querySelector('[aria-label="Preview preview-only.html"]')).not.toBeNull()
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

    expect(host.querySelector('[aria-label="Preview legacy.html"]')).toBeNull()
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

    const preview = host.querySelector<HTMLButtonElement>(
      '[aria-label^="Preview invalid.html"]',
    )
    const edit = host.querySelector<HTMLButtonElement>(
      '[aria-label^="Edit a copy of invalid.html"]',
    )
    expect(preview?.disabled).toBe(true)
    expect(edit?.disabled).toBe(true)
    const reason = host.querySelector('[data-testid="attachment-workbench-unavailable"]')
    expect(reason?.textContent).toContain('not valid UTF-8')
    expect(reason?.textContent).not.toContain('html_encoding_unsupported')
    preview?.click()
    edit?.click()
    expect(previewAttachment).not.toHaveBeenCalled()
    expect(editAttachment).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps preview low-friction while disabling an oversized HTML edit', async () => {
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

    const preview = host.querySelector<HTMLButtonElement>(
      '[aria-label="Preview large.html"]',
    )
    const edit = host.querySelector<HTMLButtonElement>(
      '[aria-label^="Edit a copy of large.html"]',
    )
    expect(preview?.disabled).toBe(false)
    expect(edit?.disabled).toBe(true)
    expect(host.querySelector('[data-testid="attachment-workbench-unavailable"]')
      ?.textContent).toContain('too large to edit')
    preview?.click()
    edit?.click()
    expect(previewAttachment).toHaveBeenCalledWith(attachment)
    expect(editAttachment).not.toHaveBeenCalled()
    app.unmount()
  })

  it('renders immutable annotation cards even when the user message has no text', async () => {
    const message = annotationMessage()
    const { app, host, reuse } = await mountAnnotationMessage(message)

    const card = host.querySelector('[data-testid="sent-prompt-annotation"]')
    expect(card?.textContent).toContain('page.html')
    expect(card?.textContent).toContain('<h1>')
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
      'Copies the modification request; then select the element again in the current version.',
    )
    expect(copyButton?.getAttribute('title')).toBe(
      'Copies the modification request; then select the element again in the current version.',
    )
    copyButton?.click()
    expect(reuse).toHaveBeenCalledWith(message.promptAnnotations?.[0])
    app.unmount()
  })

  it.each([
    {
      name: 'renders an authoritative applied receipt',
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
      name: 'renders an authoritative not-applied receipt',
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
      name: 'renders an authoritative conflict receipt',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'succeeded',
          documentMutationOutcome: { version: 1, status: 'conflict' },
        },
      }),
      expected: 'conflict',
    },
    {
      name: 'renders an authoritative ambiguous receipt',
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

    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')
      ?.getAttribute('data-status')).toBe(expected)

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
    {
      name: 'an authoritative not-attempted receipt',
      message: annotationMessage({
        messageId: 'user-1',
        turnId: 'turn-1',
        turnOutcome: {
          turnId: 'turn-1',
          status: 'succeeded',
          documentMutationOutcome: { version: 1, status: 'not_attempted' },
        },
      }),
    },
  ])('does not render a standalone status for $name', async ({ message }) => {
    const { app, host } = await mountAnnotationMessage(message)
    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')).toBeNull()
    app.unmount()
  })

  it('labels a corrected proposal only when the authoritative applied receipt says so', async () => {
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
    expect(card?.textContent).toContain('Corrected and applied')
    app.unmount()
  })
})
