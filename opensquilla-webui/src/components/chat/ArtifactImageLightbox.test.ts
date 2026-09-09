// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { DisplayAttachment } from '@/types/chat'
import {
  provideArtifactImageLightbox,
  type ArtifactImageLightboxController,
} from '@/composables/chat/useArtifactImageLightbox'
import { downloadBlob } from '@/utils/browser'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { createV4AttachmentContentAccess } from '@/adapters/gateway/attachmentAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { httpBinaryResponse, httpTransportTestDouble, type TestHttpTransport } from '@/testing/httpTransport.test-helper'
import ArtifactImageLightbox from './ArtifactImageLightbox.vue'

vi.mock('@/utils/browser', async importOriginal => ({
  ...await importOriginal<typeof import('@/utils/browser')>(),
  downloadBlob: vi.fn(),
}))

let app: App | undefined
let controller: ArtifactImageLightboxController
let objectUrls: ReturnType<typeof vi.spyOn>
let revokedUrls: ReturnType<typeof vi.spyOn>
let requestBinary: ReturnType<typeof vi.fn<TestHttpTransport['requestBinary']>>

function attachment(key: string, overrides: Partial<DisplayAttachment> = {}): DisplayAttachment {
  return {
    kind: 'inline', displayId: key, renderKey: key, name: `${key}.png`, mime: 'image/png',
    data: 'aW1hZ2U=', ...overrides,
  }
}

function mount() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  app = createApp({
    setup() {
      controller = provideArtifactImageLightbox()
      return () => h(ArtifactImageLightbox)
    },
  })
  app.use(i18n)
  const http = httpTransportTestDouble({ requestBinary })
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    content: {
      ...createV4ArtifactContentAccess(http),
      ...createV4AttachmentContentAccess(http),
    },
    previews: createV4ArtifactPreviews(http),
  } as ArtifactWorkbench)
  app.mount(host)
}

function displayedImage() {
  return document.querySelector<HTMLImageElement>('.deliv-preview__image')
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  requestBinary = vi.fn<TestHttpTransport['requestBinary']>()
  let sequence = 0
  objectUrls = vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:image-${++sequence}`)
  revokedUrls = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

afterEach(() => {
  app?.unmount()
  app = undefined
  document.body.innerHTML = ''
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('shared image preview', () => {
  it.each(['', 'application/octet-stream'])('previews a local image with inferred MIME when File.type is %j', async type => {
    mount()
    const image = attachment('inferred', {
      data: undefined,
      localFile: new File(['original'], 'inferred.png', { type }),
    })
    controller.openAttachments({ attachment: image, navigationAttachments: [image], sessionKey: '' })
    await vi.waitFor(() => expect(displayedImage()?.alt).toBe('inferred.png'))
    const blob = objectUrls.mock.calls[0][0] as Blob
    expect(blob.type).toBe('image/png')
    expect(await blob.text()).toBe('original')
  })

  it('previews local attachments, navigates, downloads explicitly and restores focus', async () => {
    mount()
    const opener = document.createElement('button')
    document.body.appendChild(opener)
    opener.focus()
    const file = new File(['original'], 'first.png', { type: 'image/png' })
    const first = attachment('first', { localFile: file })
    const second = attachment('second')
    controller.openAttachments({
      attachment: first,
      navigationAttachments: [first, second, attachment('vector', { mime: 'image/svg+xml' })],
      sessionKey: '',
    })
    await vi.waitFor(() => expect(displayedImage()?.getAttribute('src')).toBe('blob:image-1'))
    expect(objectUrls).toHaveBeenCalledWith(file)
    expect(requestBinary).not.toHaveBeenCalled()
    expect(downloadBlob).not.toHaveBeenCalled()

    // Background artifact refreshes must not replace an attachment gallery.
    controller.updateNavigation([{ id: 'generated', mime: 'image/png' }], '')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await vi.waitFor(() => expect(displayedImage()?.alt).toBe('second.png'))
    await vi.waitFor(() => expect(displayedImage()?.getAttribute('src')).toBe('blob:image-2'))
    expect(revokedUrls).toHaveBeenCalledWith('blob:image-1')
    expect(document.querySelector<HTMLButtonElement>('.deliv-preview__nav--next')?.disabled).toBe(true)
    document.querySelector<HTMLButtonElement>('.deliv-preview__actions button')?.click()
    await vi.waitFor(() => expect(downloadBlob).toHaveBeenCalledOnce())
    expect(vi.mocked(downloadBlob).mock.calls[0][1]).toBe('second.png')

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
    expect(revokedUrls).toHaveBeenCalledWith('blob:image-2')
  })

  it('loads history attachments through the scoped transport and preserves download filenames', async () => {
    mount()
    requestBinary.mockResolvedValue(httpBinaryResponse('original', {
      contentType: 'image/png', filename: 'original.png',
    }))
    const image = attachment('history', {
      kind: 'staged', data: undefined,
      download_url: '/api/v1/attachments/fixture?token=old&sessionKey=old',
    })
    controller.openAttachments({ attachment: image, navigationAttachments: [image], sessionKey: 'fixture-session' })
    await vi.waitFor(() => expect(displayedImage()).not.toBeNull())
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/attachments/fixture', expect.objectContaining({
      sessionKey: 'fixture-session', signal: expect.any(AbortSignal), timeoutMs: 0,
    }))
    expect(downloadBlob).not.toHaveBeenCalled()
    document.querySelector<HTMLButtonElement>('.deliv-preview__actions button')?.click()
    await vi.waitFor(() => expect(downloadBlob).toHaveBeenCalledOnce())
    expect(vi.mocked(downloadBlob).mock.calls[0][1]).toBe('original.png')
    document.querySelector<HTMLElement>('.deliv-preview')?.click()
    await nextTick()
    expect(controller.request.value).toBeNull()
  })

  it('keeps generated artifact previews working through the same viewer', async () => {
    mount()
    requestBinary.mockResolvedValue(httpBinaryResponse('generated', {
      contentType: 'image/png',
    }))
    const artifact = { id: 'generated', name: 'generated.png', mime: 'image/png' }
    controller.open({ artifact, navigationArtifacts: [artifact], sessionKey: 'fixture-session' })
    await vi.waitFor(() => expect(displayedImage()?.alt).toBe('generated.png'))
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/generated', expect.objectContaining({
      sessionKey: 'fixture-session',
    }))
    expect(downloadBlob).not.toHaveBeenCalled()
  })
})
