// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatMessage, ChatRenderedMessage, DisplayAttachment } from '@/types/chat'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4AttachmentContentAccess } from '@/adapters/gateway/attachmentAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { HttpTransportError } from '@/adapters/gateway/privateHttpTransport'
import { projectHistoryMessage } from '@/adapters/gateway/sessionHistoryV4'
import { normalizeDisplayAttachments } from '@/utils/chat/attachments'
import { reconcileHistoryMessages } from '@/utils/chat/historyMerge'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { httpBinaryResponse, httpTransportTestDouble, type TestHttpTransport } from '@/testing/httpTransport.test-helper'
import UserMessage from './UserMessage.vue'

const apps: App[] = []
const sessionKey = 'agent:main:webchat:synthetic-image'
const imageUrl = '/api/v1/attachments/image-1?messageId=user-1&attachmentIndex=0&revision=r1&source=active'
let requestBinary: ReturnType<typeof vi.fn<TestHttpTransport['requestBinary']>>
let objectUrls: ReturnType<typeof vi.spyOn>
let revokedUrls: ReturnType<typeof vi.spyOn>

function attachment(overrides: Partial<DisplayAttachment> = {}): DisplayAttachment {
  return { kind: 'staged', displayId: 'image-1', renderKey: 'image-1',
    name: 'synthetic.png', mime: 'image/png', download_url: imageUrl, ...overrides }
}

function message(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return { id: 'user-1', messageId: 'user-1', role: 'user', displayRole: 'user',
    roleLabel: 'You', text: '', timeStr: '', showHeader: false,
    attachments: [attachment()], ...overrides }
}

async function mount(initial = message()) {
  const current = ref(initial)
  const session = ref(sessionKey)
  const downloadAttachment = vi.fn(async () => true)
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ setup: () => () => h(UserMessage, {
    message: current.value, sessionKey: session.value, shareMode: false,
    shareSelected: false, shareMessageId: 'user-1',
    stripTimePrefix: (value: string) => value,
    copyMessage: async () => true, downloadAttachment,
  }) })
  const http = httpTransportTestDouble({ requestBinary })
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    content: createV4AttachmentContentAccess(http), previews: createV4ArtifactPreviews(http),
  } as ArtifactWorkbench)
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { current, session, host, downloadAttachment, unmount: () => {
    apps.splice(apps.indexOf(app), 1)
    app.unmount()
  } }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  vi.stubGlobal('IntersectionObserver', undefined)
  requestBinary = vi.fn<TestHttpTransport['requestBinary']>()
    .mockResolvedValue(httpBinaryResponse('synthetic image', { contentType: 'image/png' }))
  let sequence = 0
  objectUrls = vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:attachment-${++sequence}`)
  revokedUrls = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('UserMessage remote attachment thumbnails', () => {
  it.each([false, true])('shows canonical missing metadata with usable URL=%s without revoking read actions', async hasUrl => {
    const missing = normalizeDisplayAttachments([{ name: 'synthetic.png', mime: 'image/png',
      missing_reason: 'attachment preview unavailable', ...(hasUrl ? { download_url: imageUrl } : {}),
    }], { messageId: 'user-1' })
    const { host, downloadAttachment } = await mount(message({ attachments: missing }))
    const status = host.querySelector('[data-testid="attachment-workbench-unavailable"]')
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.textContent).toContain(i18n.global.t('chat.previewFailedShort'))
    expect(host.querySelector('.msg-user')).not.toBeNull()
    host.querySelector<HTMLButtonElement>('[aria-label="Download synthetic.png"]')!.click()
    expect(downloadAttachment).toHaveBeenCalledOnce()
    if (hasUrl) {
      await vi.waitFor(() => expect(host.querySelector('img.msg-thumb')).not.toBeNull())
      expect(requestBinary).toHaveBeenCalledOnce()
    } else {
      expect(requestBinary).not.toHaveBeenCalled()
      expect(host.querySelector('img')).toBeNull()
      expect(host.textContent).not.toContain(i18n.global.t('chat.loadingPreview'))
    }
  })

  it.each(['', 'Describe this synthetic image'])('keeps canonical image content visible with text=%j', async text => {
    const projected = projectHistoryMessage({ role: 'user', message_id: 'user-1', text,
      attachments: [{ name: 'synthetic.png', mime: 'image/png', download_url: imageUrl }] }, 0)
    const canonical: ChatMessage = { role: 'user', text: projected.text, ts: 1,
      messageId: projected.messageId!, restoredFromHistory: true,
      attachments: normalizeDisplayAttachments(projected.attachments, { messageId: 'user-1' }) }
    const local: ChatMessage = { role: 'user', text, ts: 1, messageId: 'user-1', clientId: 'client-1',
      attachments: [attachment({ kind: 'inline', data: 'AA==', download_url: undefined })] }
    const { renderedMessages } = useChatRenderedMessages({
      messages: ref(reconcileHistoryMessages([local], [canonical])), sessionKey: ref(sessionKey),
      routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
      renderMarkdown: value => value, stripGeneratedArtifactMarkers: value => value,
      stripTimePrefix: value => value, isSubagentCompletionMessage: () => false,
    })
    expect(renderedMessages.value).toHaveLength(1)
    const { host } = await mount(renderedMessages.value[0])
    await vi.waitFor(() => expect(host.querySelector('img.msg-thumb')?.getAttribute('src')).toBe('blob:attachment-1'))
    expect(host.querySelector('.msg-user')).not.toBeNull()
    expect(host.textContent).toContain(text)
    expect(requestBinary).toHaveBeenCalledExactlyOnceWith(imageUrl, {
      sessionKey, signal: expect.any(AbortSignal), timeoutMs: 0,
    })
    expect(host.querySelector('img')?.getAttribute('src')).not.toContain('/api/')
  })

  it('waits for visibility and reuses the controller after an equivalent canonical object replacement', async () => {
    let intersect!: IntersectionObserverCallback
    const disconnect = vi.fn()
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { intersect = callback }
      observe() {}
      unobserve() {}
      disconnect = disconnect
    })
    const { host, current, unmount } = await mount()
    expect(requestBinary).not.toHaveBeenCalled()
    intersect([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver)
    await vi.waitFor(() => expect(host.querySelector('img.msg-thumb')).not.toBeNull())
    current.value = message({ attachments: [{ ...attachment() }] })
    await nextTick()
    expect(requestBinary).toHaveBeenCalledOnce()
    unmount()
    expect(revokedUrls).toHaveBeenCalledExactlyOnceWith('blob:attachment-1')
    expect(disconnect).toHaveBeenCalled()
  })

  it('retires object URLs when source, session, or attachment presence changes', async () => {
    const { host, current, session } = await mount()
    await vi.waitFor(() => expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:attachment-1'))
    current.value = message({ attachments: [attachment({ download_url: imageUrl.replace('r1', 'r2') })] })
    await vi.waitFor(() => expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:attachment-2'))
    expect(revokedUrls).toHaveBeenCalledWith('blob:attachment-1')
    session.value = 'agent:main:webchat:other-synthetic'
    await vi.waitFor(() => expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:attachment-3'))
    expect(revokedUrls).toHaveBeenCalledWith('blob:attachment-2')
    expect(requestBinary.mock.calls[2]?.[1]?.sessionKey).toBe(session.value)
    current.value = message({ attachments: [] })
    await nextTick()
    expect(revokedUrls).toHaveBeenCalledWith('blob:attachment-3')
  })

  it('aborts on unmount and rejects late bytes without creating a URL', async () => {
    let resolve!: (value: Awaited<ReturnType<TestHttpTransport['requestBinary']>>) => void
    requestBinary.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const { unmount } = await mount()
    await vi.waitFor(() => expect(requestBinary).toHaveBeenCalledOnce())
    const signal = requestBinary.mock.calls[0]?.[1]?.signal!
    unmount()
    expect(signal.aborted).toBe(true)
    resolve(httpBinaryResponse('late image', { contentType: 'image/png' }))
    await new Promise(done => setTimeout(done, 0))
    expect(objectUrls).not.toHaveBeenCalled()
  })

  it.each([403, 404, 500])('keeps the message and attachment actions when reading returns HTTP %s', async status => {
    requestBinary.mockRejectedValueOnce(new HttpTransportError('http-status', 'Synthetic read failure', status))
    const { host } = await mount()
    await vi.waitFor(() => expect(host.textContent).toContain(i18n.global.t('chat.previewFailedShort')))
    expect(host.querySelector('.msg-user')).not.toBeNull()
    expect(host.textContent).toContain('synthetic.png')
    expect(host.querySelector('[aria-label="Open synthetic.png"]')).not.toBeNull()
    expect(host.querySelector('[aria-label="Download synthetic.png"]')).not.toBeNull()
    expect(host.querySelector('[aria-label="Retry preview for synthetic.png"]')).not.toBeNull()
    expect(objectUrls).not.toHaveBeenCalled()
    expect(requestBinary).toHaveBeenCalledOnce()
  })

  it('retries a failed preview only through the existing explicit action', async () => {
    requestBinary.mockRejectedValueOnce(new HttpTransportError('http-status', 'Synthetic read failure', 503))
    const { host } = await mount()
    await vi.waitFor(() => expect(host.textContent).toContain(i18n.global.t('chat.previewFailedShort')))
    host.querySelector<HTMLButtonElement>('[aria-label="Retry preview for synthetic.png"]')!.click()
    await vi.waitFor(() => expect(host.querySelector('img.msg-thumb')?.getAttribute('src')).toBe('blob:attachment-1'))
    expect(requestBinary).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['oversize header', () => httpBinaryResponse('small', { contentType: 'image/png', contentLength: 5 * 1024 * 1024 + 1 })],
    ['oversize body', () => httpBinaryResponse(new Blob([new Uint8Array(5 * 1024 * 1024 + 1)], { type: 'image/png' }), { contentLength: 1 })],
    ['non-image response', () => httpBinaryResponse('not an image', { contentType: 'text/html' })],
  ])('rejects %s without hiding the attachment', async (_label, response) => {
    requestBinary.mockResolvedValueOnce(response())
    const { host } = await mount()
    await vi.waitFor(() => expect(host.textContent).toContain(i18n.global.t('chat.previewFailedShort')))
    expect(host.querySelector('.msg-attachments')).not.toBeNull()
    expect(objectUrls).not.toHaveBeenCalled()
  })

  it('rejects an untrusted remote URL through the existing attachment port', async () => {
    const { host } = await mount(message({ attachments: [attachment({ download_url: 'https://untrusted.example/image.png' })] }))
    await vi.waitFor(() => expect(host.textContent).toContain(i18n.global.t('chat.previewFailedShort')))
    expect(requestBinary).not.toHaveBeenCalled()
    expect(host.querySelector('img')).toBeNull()
  })

  it.each([
    { data: 'AA==' }, { dataUrl: 'data:image/png;base64,AA==' },
  ])('keeps the old inline image path and never fetches even when a URL also exists: %j', async inline => {
    const { host } = await mount(message({ attachments: [attachment({ kind: 'inline', ...inline })] }))
    expect(host.querySelector('img.msg-thumb')?.getAttribute('src')).toBe('data:image/png;base64,AA==')
    expect(requestBinary).not.toHaveBeenCalled()
    expect(objectUrls).not.toHaveBeenCalled()
  })
})
