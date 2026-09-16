// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { Attachment } from '@/types/chat'
import ChatComposer from './ChatComposer.vue'

const apps: App[] = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function imageAttachment(overrides: Partial<Attachment> = {}): Attachment {
  return {
    kind: 'inline',
    local_id: 1,
    name: 'sample.png',
    mime: 'image/png',
    data: 'aW1hZ2U=',
    dataUrl: 'data:image/png;base64,aW1hZ2U=',
    ...overrides,
  }
}

async function mountAttachment(attachment: Attachment) {
  const previewImage = vi.fn()
  const send = vi.fn()
  const removeAttachment = vi.fn()
  const retryAttachment = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(ChatComposer, {
    modelValue: 'Describe the image',
    'onUpdate:modelValue': () => {},
    attachments: [attachment],
    busySendMode: 'queue',
    hasSendContent: true,
    isStreaming: false,
    canStop: false,
    isNewLanding: false,
    placeholder: 'Send a message',
    sendButtonTitle: 'Send',
    runMode: 'trusted',
    allowedRunModes: ['standard', 'trusted', 'full'],
    runModeLocked: false,
    runModeLockMessage: '',
    sessionRoutingMode: 'llm_ensemble',
    sessionRoutingBusy: false,
    voiceBusy: false,
    voiceRecording: false,
    voiceReady: true,
    onPreviewImage: previewImage,
    onSend: send,
    onRemoveAttachment: removeAttachment,
    onRetryAttachment: retryAttachment,
  })
  apps.push(app)
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { host, previewImage, send, removeAttachment, retryAttachment }
}

describe('ChatComposer uploaded image preview', () => {
  it.each(['inline', 'data URL', 'staged'] as const)('opens an unsent %s image without sending or removing it', async (source) => {
    const attachment = imageAttachment(source === 'staged'
      ? { kind: 'staged', data: undefined, dataUrl: undefined, file_uuid: 'file-1', file: new File(['image'], 'sample.png', { type: 'image/png' }) }
      : source === 'data URL' ? { data: undefined } : {})
    const { host, previewImage, send, removeAttachment, retryAttachment } = await mountAttachment(attachment)
    const preview = host.querySelector<HTMLButtonElement>('.attachment-chip__preview')
    expect(preview?.tagName).toBe('BUTTON')
    expect(preview?.type).toBe('button')
    expect(preview?.getAttribute('aria-label')).toBe('Open sample.png')
    expect(preview?.querySelector('.attachment-chip__name')?.textContent).toBe('sample.png')
    if (source === 'staged') expect(preview?.querySelector('img')).toBeNull()

    preview?.click()
    expect(previewImage).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(send).not.toHaveBeenCalled()
    expect(removeAttachment).not.toHaveBeenCalled()
    expect(retryAttachment).not.toHaveBeenCalled()

    const remove = host.querySelector<HTMLButtonElement>('.attachment-remove')
    expect(preview?.contains(remove)).toBe(false)
    remove?.click()
    expect(removeAttachment).toHaveBeenCalledExactlyOnceWith(0)
    expect(previewImage).toHaveBeenCalledOnce()
  })

  it('keeps retry separate from the local image preview after an upload fails', async () => {
    const attachment = imageAttachment({
      kind: 'failed',
      data: undefined,
      dataUrl: undefined,
      file: new File(['image'], 'sample.png', { type: 'image/png' }),
    })
    const { host, previewImage, send, removeAttachment, retryAttachment } = await mountAttachment(attachment)
    const retry = host.querySelector<HTMLButtonElement>('[aria-label="Retry upload"]')
    expect(retry).not.toBeNull()
    retry?.click()
    expect(retryAttachment).toHaveBeenCalledExactlyOnceWith(0)
    expect(previewImage).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
    expect(removeAttachment).not.toHaveBeenCalled()
  })

  it.each([
    ['drawing.svg', 'image/svg+xml', 'aW1hZ2U='],
    ['report.pdf', 'application/pdf', 'ZG9j'],
    ['unavailable.png', 'image/png', undefined],
  ])('does not offer an image preview for %s', async (name, mime, data) => {
    const attachment = imageAttachment({ name, mime, data, dataUrl: undefined })
    const { host, previewImage, send } = await mountAttachment(attachment)
    expect(host.querySelector('.attachment-chip__preview')).toBeNull()
    host.querySelector<HTMLElement>('.attachment-chip__name')?.click()
    expect(previewImage).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
  })
})
