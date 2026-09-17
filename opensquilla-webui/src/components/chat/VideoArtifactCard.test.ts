// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { HttpTransportError } from '@/adapters/gateway/privateHttpTransport'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import {
  httpBinaryResponse,
  httpTransportTestDouble,
  type TestHttpBinaryResponse,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import type { ArtifactPayload } from '@/types/artifacts'
import VideoArtifactCard from './VideoArtifactCard.vue'

const artifact: ArtifactPayload = {
  id: 'video-1',
  name: 'clip.webm',
  mime: 'video/webm',
  download_url: '/api/v1/artifacts/video-1?token=old',
}

async function settle() {
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await Promise.resolve()
  await nextTick()
}

async function mountCard(
  http: TestHttpTransport,
  onDownload = vi.fn(),
  item: ArtifactPayload = artifact,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(VideoArtifactCard, {
    artifact: item,
    sessionKey: 'agent:main:webchat:ok',
    onDownload,
  })
  app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    previews: createV4ArtifactPreviews(http, { baseOrigin: () => 'http://localhost' }),
  } as ArtifactWorkbench)
  app.mount(el)
  await nextTick()
  return { app, el, onDownload }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.stubGlobal('sessionStorage', {
    getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? 'secret' : null),
  })
})

describe('VideoArtifactCard', () => {
  it('loads only after Play, authenticates the request, and releases the Blob URL', async () => {
    const requestBinary = vi.fn(async () => httpBinaryResponse('video-bytes', {
      contentType: 'video/webm',
    }))
    const http = httpTransportTestDouble({ requestBinary })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-1')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard(http)

    expect(requestBinary).not.toHaveBeenCalled()
    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()

    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/video-1', {
      sessionKey: 'agent:main:webchat:ok',
      signal: expect.any(AbortSignal),
      timeoutMs: 0,
    })
    expect(createObjectUrl).toHaveBeenCalledOnce()
    const player = el.querySelector<HTMLVideoElement>('.msg-video-card__player')
    expect(player?.src).toContain('blob:video-1')
    expect(player?.hasAttribute('controls')).toBe(true)
    expect(player?.hasAttribute('playsinline')).toBe(true)
    expect(player?.preload).toBe('metadata')

    app.unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:video-1')
  })

  it('rejects cross-origin video instead of making an unauthenticated request', async () => {
    const requestBinary = vi.fn()
    const http = httpTransportTestDouble({ requestBinary })
    const { app, el } = await mountCard(http, vi.fn(), {
      ...artifact,
      download_url: 'https://files.example.test/video/clip.webm?token=secret',
    })

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()

    expect(requestBinary).not.toHaveBeenCalled()
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('error')
    app.unmount()
  })

  it('offers Retry after a fetch failure', async () => {
    const requestBinary = vi.fn()
      .mockRejectedValueOnce(new HttpTransportError('http-status', 'missing', 404))
      .mockResolvedValueOnce(httpBinaryResponse('video', { contentType: 'video/webm' }))
    const http = httpTransportTestDouble({ requestBinary })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-retry')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard(http)

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('error')

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(requestBinary).toHaveBeenCalledTimes(2)
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('ready')
    app.unmount()
  })

  it('falls back to Download when the browser rejects the codec', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(async () => httpBinaryResponse('video', {
        contentType: 'video/x-unknown',
      })),
    })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('')
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:unused')
    const { app, el, onDownload } = await mountCard(http)

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('unsupported')
    expect(createObjectUrl).not.toHaveBeenCalled()

    el.querySelector<HTMLButtonElement>('.msg-video-card__download')?.click()
    expect(onDownload).toHaveBeenCalledWith(artifact)
    app.unmount()
  })

  it('aborts an in-flight request when the card unmounts', async () => {
    let requestSignal: AbortSignal | undefined
    const http = httpTransportTestDouble({
      requestBinary: vi.fn((_url, options) => {
        requestSignal = options?.signal
        return new Promise<TestHttpBinaryResponse>(() => undefined)
      }),
    })
    const { app, el } = await mountCard(http)

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await Promise.resolve()
    expect(requestSignal?.aborted).toBe(false)

    app.unmount()
    expect(requestSignal?.aborted).toBe(true)
  })

  it('revokes loaded video when session context changes', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(async () => httpBinaryResponse('video', {
        contentType: 'video/webm',
      })),
    })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-session')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const sessionKey = ref('agent:main:webchat:one')
    const Root = defineComponent({
      setup: () => () => h(VideoArtifactCard, {
        artifact,
        sessionKey: sessionKey.value,
      }),
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(Root)
    app.use(i18n)
    app.provide(ARTIFACT_WORKBENCH_KEY, {
      previews: createV4ArtifactPreviews(http, { baseOrigin: () => 'http://localhost' }),
    } as ArtifactWorkbench)
    app.mount(host)
    await nextTick()

    host.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(host.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('ready')

    sessionKey.value = 'agent:main:webchat:two'
    await nextTick()
    expect(host.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('idle')
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:video-session')
    app.unmount()
  })
})
