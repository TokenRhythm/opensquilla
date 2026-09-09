// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage, DisplayAttachment } from '@/types/chat'
import UserMessage from './UserMessage.vue'

const apps: App[] = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function imageAttachment(overrides: Partial<DisplayAttachment> = {}): DisplayAttachment {
  return {
    kind: 'inline',
    displayId: 'image-1',
    renderKey: 'image-1',
    name: 'sample.png',
    mime: 'image/png',
    data: 'aW1hZ2U=',
    ...overrides,
  }
}

async function mountAttachment(attachment: DisplayAttachment, shareMode = false) {
  const message: ChatRenderedMessage = {
    id: 'message-1',
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: '',
    timeStr: '',
    showHeader: false,
    attachments: [attachment],
  }
  const previewImage = vi.fn()
  const downloadAttachment = vi.fn(async () => true)
  const toggleShare = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(UserMessage, {
    message,
    shareMode,
    shareSelected: false,
    shareMessageId: message.id,
    stripTimePrefix: (value: string) => value,
    copyMessage: async () => true,
    downloadAttachment,
    onPreviewImage: previewImage,
    onToggleShare: toggleShare,
  })
  apps.push(app)
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { host, previewImage, downloadAttachment, toggleShare }
}

describe('UserMessage uploaded image preview', () => {
  it.each<[string, DisplayAttachment]>([
    ['inline thumbnail', imageAttachment()],
    ['data URL thumbnail', imageAttachment({ data: undefined, dataUrl: 'data:image/png;base64,aW1hZ2U=' })],
    ['staged image chip', imageAttachment({ kind: 'staged', data: undefined, download_url: '/api/v1/attachments/image-1' })],
  ])('opens a %s and preserves a separate download action', async (_name, attachment) => {
    const { host, previewImage, downloadAttachment } = await mountAttachment(attachment)
    const open = host.querySelector<HTMLButtonElement>('[aria-label="Open sample.png"]')
    const download = host.querySelector<HTMLButtonElement>('[aria-label="Download sample.png"]')
    expect(open?.tagName).toBe('BUTTON')
    expect(open?.type).toBe('button')
    expect(download).not.toBeNull()
    expect(open?.contains(download)).toBe(false)
    expect(download?.contains(open)).toBe(false)

    open?.click()
    expect(previewImage).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(downloadAttachment).not.toHaveBeenCalled()

    download?.click()
    expect(downloadAttachment).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(previewImage).toHaveBeenCalledOnce()
  })

  it('opens the image without toggling message selection in share mode', async () => {
    const attachment = imageAttachment()
    const { host, previewImage, downloadAttachment, toggleShare } = await mountAttachment(attachment, true)
    host.querySelector<HTMLButtonElement>('.msg-thumb-button')?.click()
    expect(previewImage).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(downloadAttachment).not.toHaveBeenCalled()
    expect(toggleShare).not.toHaveBeenCalled()
  })

  it.each([
    ['drawing.svg', 'image/svg+xml'],
    ['report.pdf', 'application/pdf'],
  ])('keeps %s on its existing download action', async (name, mime) => {
    const attachment = imageAttachment({ name, mime })
    const { host, previewImage, downloadAttachment } = await mountAttachment(attachment)
    expect(host.querySelector('.msg-thumb')).toBeNull()
    const download = host.querySelector<HTMLButtonElement>(`[aria-label="Download ${name}"]`)
    expect(download).not.toBeNull()
    download?.click()
    expect(downloadAttachment).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(previewImage).not.toHaveBeenCalled()
  })
})
