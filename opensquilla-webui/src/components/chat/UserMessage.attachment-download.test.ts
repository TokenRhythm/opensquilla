// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import type { WorkbenchResource } from '@/types/workbenchResources'
import UserMessage from './UserMessage.vue'

const message = {
  id: 'message-1',
  role: 'user',
  displayRole: 'user',
  roleLabel: 'You',
  text: 'attached',
  timeStr: '',
  showHeader: false,
  attachments: [{
    kind: 'file',
    displayId: 'attachment-1',
    renderKey: 'attachment-1',
    name: 'report.pdf',
    mime: 'application/pdf',
  }],
} satisfies ChatRenderedMessage

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('UserMessage attachment download', () => {
  it.each([true, false])('uses the general edit capability to open HTML: %s', async (edit) => {
    const attachment = {
      ...message.attachments[0],
      attachmentId: 'html-attachment',
      name: 'page.html',
      mime: 'text/html',
    }
    const resource: WorkbenchResource = {
      resource: { type: 'attachment', id: attachment.attachmentId },
      name: attachment.name,
      mime: attachment.mime,
      capabilities: {
        preview: false, download: true, selectionContext: false,
        manualEdit: !edit, agentEdit: false, edit, publish: false,
      },
      relations: {},
    }
    const previewAttachment = vi.fn()
    const downloadAttachment = vi.fn(async () => true)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message: { ...message, attachments: [attachment] },
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment,
      workbenchResourceEditEnabled: true,
      workbenchAttachmentResources: new Map([[attachment.attachmentId, resource]]),
      onPreviewAttachment: previewAttachment,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    host.querySelector<HTMLButtonElement>('.msg-file-chip')?.click()
    await nextTick()

    expect(previewAttachment).toHaveBeenCalledTimes(edit ? 1 : 0)
    expect(downloadAttachment).toHaveBeenCalledTimes(edit ? 0 : 1)
    app.unmount()
  })

  it('uses a busy deduped button and leaves share selection untouched', async () => {
    let resolveDownload: ((ok: boolean) => void) | undefined
    const downloadAttachment = vi.fn(() => new Promise<boolean>((resolve) => {
      resolveDownload = resolve
    }))
    const toggleShare = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message,
      shareMode: true,
      shareSelected: false,
      shareMessageId: 'message-1',
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment,
      onToggleShare: toggleShare,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const chip = host.querySelector<HTMLButtonElement>('.msg-file-chip')
    chip?.click()
    chip?.click()
    await nextTick()

    expect(downloadAttachment).toHaveBeenCalledOnce()
    expect(toggleShare).not.toHaveBeenCalled()
    expect(chip?.disabled).toBe(true)
    expect(chip?.getAttribute('aria-busy')).toBe('true')

    resolveDownload?.(false)
    await Promise.resolve()
    await nextTick()
    expect(chip?.disabled).toBe(false)
    expect(chip?.classList.contains('msg-file-chip--failed')).toBe(true)

    chip?.click()
    expect(downloadAttachment).toHaveBeenCalledTimes(2)
    expect(toggleShare).not.toHaveBeenCalled()
    app.unmount()
  })
})
