// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import type { ArtifactPayload } from '@/types/rpc'
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

async function mountCard(onDownload = vi.fn(), item: ArtifactPayload = artifact) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(VideoArtifactCard, {
    artifact: item,
    sessionKey: 'agent:main:webchat:ok',
    authToken: 'secret',
    onDownload,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, onDownload }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('VideoArtifactCard', () => {
  it('loads only after Play, authenticates the request, and releases the Blob URL', async () => {
    const fetchImpl = vi.fn(async () => new Response('video-bytes', {
      status: 200,
      headers: { 'content-type': 'video/webm' },
    }))
    vi.stubGlobal('fetch', fetchImpl)
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-1')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard()

    expect(fetchImpl).not.toHaveBeenCalled()
    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()

    expect(fetchImpl).toHaveBeenCalledWith('/api/v1/artifacts/video-1', {
      method: 'GET',
      headers: {
        'x-opensquilla-session-key': 'agent:main:webchat:ok',
        Authorization: 'Bearer secret',
      },
      credentials: 'same-origin',
      signal: expect.any(AbortSignal),
      redirect: 'error',
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
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const { app, el } = await mountCard(vi.fn(), {
      ...artifact,
      download_url: 'https://files.example.test/video/clip.webm?token=secret',
    })

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('error')
    app.unmount()
  })

  it('offers Retry after a fetch failure', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response('missing', { status: 404 }))
      .mockResolvedValueOnce(new Response('video', {
        status: 200,
        headers: { 'content-type': 'video/webm' },
      }))
    vi.stubGlobal('fetch', fetchImpl)
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-retry')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard()

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('error')

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(el.querySelector('.msg-video-card')?.getAttribute('data-state')).toBe('ready')
    app.unmount()
  })

  it('falls back to Download when the browser rejects the codec', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('video', {
      status: 200,
      headers: { 'content-type': 'video/x-unknown' },
    })))
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('')
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:unused')
    const { app, el, onDownload } = await mountCard()

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
    vi.stubGlobal('fetch', vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      requestSignal = init?.signal || undefined
      return new Promise<Response>(() => {})
    }))
    const { app, el } = await mountCard()

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await Promise.resolve()
    expect(requestSignal?.aborted).toBe(false)

    app.unmount()
    expect(requestSignal?.aborted).toBe(true)
  })

  it('revokes loaded video when session context changes', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('video', {
      status: 200,
      headers: { 'content-type': 'video/webm' },
    })))
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:video-session')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const sessionKey = ref('agent:main:webchat:one')
    const Root = defineComponent({
      setup: () => () => h(VideoArtifactCard, {
        artifact,
        sessionKey: sessionKey.value,
        authToken: 'secret',
      }),
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(Root)
    app.use(i18n)
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
