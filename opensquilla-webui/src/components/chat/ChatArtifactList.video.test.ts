// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import type { ArtifactPayload } from '@/types/rpc'
import ChatArtifactList from './ChatArtifactList.vue'

const videoArtifact: ArtifactPayload = {
  id: 'art-video',
  name: 'final_subtitled.mp4',
  mime: 'video/mp4',
  size: 4096,
  download_url: '/api/v1/artifacts/art-video',
}

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

async function mountList(
  onDownload = vi.fn(),
  artifacts: ArtifactPayload[] = [videoArtifact],
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const pinia = createPinia()
  setActivePinia(pinia)
  const app = createApp(ChatArtifactList, {
    artifacts,
    sessionKey: 'agent:main:webchat:video',
    authToken: 'secret',
    onDownload,
  })
  app.use(pinia)
  app.use(i18n)
  app.mount(el)
  await settle()
  return { app, el, onDownload }
}

async function requestPreview(el: HTMLElement) {
  el.querySelector<HTMLButtonElement>('.msg-video-card__load')?.click()
  await settle()
}

function successfulVideoResponse() {
  return new Response(new Blob(['video-bytes'], { type: 'video/mp4' }), {
    status: 200,
    headers: {
      'Content-Length': '11',
      'Content-Type': 'video/mp4',
    },
  })
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:opensquilla-video')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
})

describe('ChatArtifactList video previews', () => {
  it('renders an authenticated inline player and preserves download', async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      successfulVideoResponse())
    vi.stubGlobal('fetch', fetchImpl)
    const { app, el, onDownload } = await mountList()

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(el.querySelector('.msg-video-card__load')?.textContent).toContain('Load video preview')
    await requestPreview(el)

    expect(fetchImpl).toHaveBeenCalledWith('/api/v1/artifacts/art-video', expect.objectContaining({
      method: 'GET',
      credentials: 'same-origin',
      headers: {
        'x-opensquilla-session-key': 'agent:main:webchat:video',
        Authorization: 'Bearer secret',
      },
    }))
    expect(String(fetchImpl.mock.calls[0]?.[0])).not.toContain('token=')
    expect(String(fetchImpl.mock.calls[0]?.[0])).not.toContain('sessionKey=')

    const player = el.querySelector<HTMLVideoElement>('.msg-video-card__video')
    expect(player).toBeTruthy()
    expect(player?.getAttribute('src')).toBe('blob:opensquilla-video')
    expect(player?.hasAttribute('controls')).toBe(true)
    expect(player?.hasAttribute('playsinline')).toBe(true)
    expect(player?.getAttribute('preload')).toBe('metadata')
    expect(player?.getAttribute('aria-label')).toContain('final_subtitled.mp4')
    expect(el.querySelector('.msg-artifact-chip')).toBeNull()

    el.querySelector<HTMLButtonElement>('.msg-video-card__download')?.click()
    expect(onDownload).toHaveBeenCalledWith(videoArtifact)

    app.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:opensquilla-video')
  })

  it('shows a browser playback fallback while keeping retry and download available', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => successfulVideoResponse()))
    const { app, el, onDownload } = await mountList()
    await requestPreview(el)

    el.querySelector<HTMLVideoElement>('.msg-video-card__video')
      ?.dispatchEvent(new Event('error'))
    await nextTick()

    expect(el.querySelector('.msg-video-card__video')).toBeNull()
    expect(el.querySelector('.msg-video-card__fallback')?.textContent)
      .toContain('This browser cannot play this video')
    const fallbackButtons = el.querySelectorAll<HTMLButtonElement>('.msg-video-card__fallback .msg-video-card__retry')
    expect(fallbackButtons).toHaveLength(2)
    fallbackButtons[1]?.click()
    expect(onDownload).toHaveBeenCalledWith(videoArtifact)

    fallbackButtons[0]?.click()
    await nextTick()
    expect(el.querySelector('.msg-video-card__video')).toBeTruthy()
    app.unmount()
  })

  it('recovers from a failed authenticated preview fetch', async () => {
    vi.useFakeTimers()
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response('', { status: 503 }))
      .mockResolvedValueOnce(successfulVideoResponse())
    vi.stubGlobal('fetch', fetchImpl)
    const { app, el } = await mountList()
    await requestPreview(el)

    expect(el.querySelector('.msg-video-card__fallback')?.textContent)
      .toContain('Preview failed to load')
    el.querySelector<HTMLButtonElement>('.msg-video-card__fallback .msg-video-card__retry')?.click()
    await vi.advanceTimersByTimeAsync(300)
    await settle()

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(el.querySelector('.msg-video-card__video')).toBeTruthy()
    app.unmount()
  })

  it('lets the user explicitly unload a buffered video preview', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => successfulVideoResponse()))
    const { app, el } = await mountList()
    await requestPreview(el)

    expect(el.querySelector('.msg-video-card__video')).toBeTruthy()
    el.querySelector<HTMLButtonElement>('[data-testid="video-preview-unload"]')?.click()
    await settle()

    expect(el.querySelector('.msg-video-card__video')).toBeNull()
    expect(el.querySelector('.msg-video-card__load')?.textContent).toContain('Load video preview')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:opensquilla-video')
    app.unmount()
  })

  it('rejects a video that exceeds the bounded preview response size', async () => {
    const tooLarge = 128 * 1024 * 1024 + 1
    vi.stubGlobal('fetch', vi.fn(async () => new Response('small-test-body', {
      status: 200,
      headers: {
        'Content-Length': String(tooLarge),
        'Content-Type': 'video/mp4',
      },
    })))
    const { app, el } = await mountList()
    await requestPreview(el)

    expect(el.querySelector('.msg-video-card__fallback')?.textContent)
      .toContain('larger than the 128 MB preview limit')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    app.unmount()
  })

  it('shows slow-load guidance and cancels the active request', async () => {
    let requestSignal: AbortSignal | null = null
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal || null
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'))
        })
      })
    }))
    const activeRequestWasAborted = () => requestSignal?.aborted === true
    const { app, el } = await mountList()

    el.querySelector<HTMLButtonElement>('.msg-video-card__load')?.click()
    await settle()
    expect(el.querySelector('.msg-video-card__loading')?.textContent)
      .toContain('You can cancel while it downloads')

    el.querySelector<HTMLButtonElement>('[data-testid="video-preview-cancel"]')?.click()
    await settle()

    expect(activeRequestWasAborted()).toBe(true)
    expect(el.querySelector('.msg-video-card__load')).toBeTruthy()
    expect(el.querySelector('.msg-video-card__fallback')).toBeNull()
    app.unmount()
  })

  it('retains at most two loaded video previews and releases the oldest', async () => {
    const artifacts = [0, 1, 2].map(index => ({
      ...videoArtifact,
      id: `art-video-${index}`,
      name: `scene-${index}.mp4`,
      download_url: `/api/v1/artifacts/art-video-${index}`,
    }))
    let urlIndex = 0
    vi.mocked(URL.createObjectURL).mockImplementation(() => `blob:opensquilla-video-${urlIndex += 1}`)
    vi.stubGlobal('fetch', vi.fn(async () => successfulVideoResponse()))
    const { app, el } = await mountList(vi.fn(), artifacts)
    const cards = [...el.querySelectorAll<HTMLElement>('.msg-video-card')]

    for (const card of cards) {
      card.querySelector<HTMLButtonElement>('.msg-video-card__load')?.click()
      await settle()
    }

    expect(el.querySelectorAll('.msg-video-card__video')).toHaveLength(2)
    expect(cards[0]?.querySelector('.msg-video-card__load')).toBeTruthy()
    expect(cards[1]?.querySelector('.msg-video-card__video')).toBeTruthy()
    expect(cards[2]?.querySelector('.msg-video-card__video')).toBeTruthy()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:opensquilla-video-1')
    app.unmount()
  })
})
