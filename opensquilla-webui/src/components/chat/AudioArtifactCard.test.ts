// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import type { ArtifactPayload } from '@/types/rpc'
import AudioArtifactCard from './AudioArtifactCard.vue'

const artifact: ArtifactPayload = {
  id: 'audio-1',
  name: 'answer.mp3',
  mime: 'audio/mpeg',
  download_url: '/api/v1/artifacts/audio-1?token=old',
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
  const app = createApp(AudioArtifactCard, {
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

describe('AudioArtifactCard', () => {
  it('does not fetch until Play, then uses authenticated Blob audio and revokes it', async () => {
    const fetchImpl = vi.fn(async () => new Response('audio-bytes', {
      status: 200,
      headers: { 'content-type': 'audio/mpeg' },
    }))
    vi.stubGlobal('fetch', fetchImpl)
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-1')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard()

    expect(fetchImpl).not.toHaveBeenCalled()
    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()

    expect(fetchImpl).toHaveBeenCalledWith('/api/v1/artifacts/audio-1', {
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
    expect(el.querySelector<HTMLAudioElement>('.msg-audio-card__player')?.src).toContain('blob:audio-1')

    app.unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:audio-1')
  })

  it('rejects cross-origin audio instead of falling back to an unauthenticated fetch', async () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const crossOrigin = {
      ...artifact,
      download_url: 'https://files.example.test/audio/answer.mp3?token=secret',
    }
    const { app, el } = await mountCard(vi.fn(), crossOrigin)

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('error')
    app.unmount()
  })

  it('offers Retry after a fetch failure', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response('missing', { status: 404 }))
      .mockResolvedValueOnce(new Response('audio', {
        status: 200,
        headers: { 'content-type': 'audio/mpeg' },
      }))
    vi.stubGlobal('fetch', fetchImpl)
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-retry')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard()

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('error')

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('ready')
    app.unmount()
  })

  it('falls back to Download when the browser rejects the codec', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('audio', {
      status: 200,
      headers: { 'content-type': 'audio/x-unknown' },
    })))
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('')
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:unused')
    const { app, el, onDownload } = await mountCard()

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('unsupported')
    expect(createObjectUrl).not.toHaveBeenCalled()

    el.querySelector<HTMLButtonElement>('.msg-audio-card__download')?.click()
    expect(onDownload).toHaveBeenCalledWith(artifact)
    app.unmount()
  })

  it('aborts an in-flight audio request when the card unmounts', async () => {
    let requestSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      requestSignal = init?.signal || undefined
      return new Promise<Response>(() => {})
    }))
    const { app, el } = await mountCard()

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await Promise.resolve()
    expect(requestSignal?.aborted).toBe(false)

    app.unmount()
    expect(requestSignal?.aborted).toBe(true)
  })

  it('revokes loaded audio when session context changes', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('audio', {
      status: 200,
      headers: { 'content-type': 'audio/mpeg' },
    })))
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-session')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const sessionKey = ref('agent:main:webchat:one')
    const Root = defineComponent({
      setup: () => () => h(AudioArtifactCard, {
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

    host.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(host.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('ready')

    sessionKey.value = 'agent:main:webchat:two'
    await nextTick()
    expect(host.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('idle')
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:audio-session')
    app.unmount()
  })
})
