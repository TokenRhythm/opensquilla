// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
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
  it.each(['application/pdf', 'image/png'])('shows the live project target without inventing a download for %s', async mime => {
    const attachment = { ...message.attachments[0], mime,
      workspaceFile: { workspaceId: 'project-fixture', relativePath: 'research/report.pdf',
        name: 'report.pdf', mime },
    }
    const downloadAttachment = vi.fn(async () => true)
    const previewImage = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message: { ...message, attachments: [attachment] },
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true, downloadAttachment, onPreviewImage: previewImage,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const chip = host.querySelector<HTMLElement>('.msg-file-chip')!
    expect(chip.tagName).toBe('SPAN')
    expect(chip.textContent).toContain('Live project file: research/report.pdf')
    expect(host.querySelector('.msg-attachments button')).toBeNull()
    chip.click()
    expect(downloadAttachment).not.toHaveBeenCalled()
    expect(previewImage).not.toHaveBeenCalled()
    expect(chip.textContent).not.toContain('Imported')
    app.unmount()
  })

  it('explains an imported input working copy while retaining its original download', async () => {
    const attachment = { ...message.attachments[0], kind: 'staged' as const,
      download_url: '/api/v1/attachments/fixture-input' }
    const downloadAttachment = vi.fn(async () => true)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message: { ...message, attachments: [attachment] },
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true, downloadAttachment,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()
    const chip = host.querySelector<HTMLButtonElement>('.msg-file-chip')!
    expect(chip.tagName).toBe('BUTTON')
    expect(chip.getAttribute('aria-label')).toBe('Download report.pdf')
    expect(chip.textContent).toContain('Imported file; edits use a working copy')
    chip.click()
    expect(downloadAttachment).toHaveBeenCalledExactlyOnceWith(attachment)
    app.unmount()
  })

  it('does not infer a working-copy target from generic file metadata', async () => {
    const host = document.createElement('div')
    const app = createApp(UserMessage, { message,
      stripTimePrefix: (value: string) => value, copyMessage: async () => true,
      downloadAttachment: async () => true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()
    expect(host.querySelector('.msg-file-chip__target')).toBeNull()
    app.unmount()
  })

  it('shows concise Office labels, sizes, and a localized unknown-file label', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const attachments = [
      {
        ...message.attachments[0],
        name: 'sample.docx',
        mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        size: 2048,
      },
      {
        ...message.attachments[0],
        displayId: 'attachment-2',
        renderKey: 'attachment-2',
        name: 'sample',
        mime: 'application/octet-stream',
      },
    ]
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message: { ...message, attachments },
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(Array.from(host.querySelectorAll('.msg-file-chip__meta'), el => el.textContent))
      .toEqual(['DOCX · 2 KB', '文件'])
    app.unmount()
  })

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
