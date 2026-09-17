// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import type { ArtifactPayload } from '@/types/artifacts'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { HttpTransportError } from '@/adapters/gateway/privateHttpTransport'
import {
  httpBinaryResponse,
  httpTransportTestDouble,
  type TestHttpBinaryResponse,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
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

async function mountCard(
  http: TestHttpTransport,
  onDownload = vi.fn(),
  item: ArtifactPayload = artifact,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(AudioArtifactCard, {
    artifact: item,
    sessionKey: 'agent:main:webchat:ok',
    onDownload,
  })
  app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    content: createV4ArtifactContentAccess(http),
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

describe('AudioArtifactCard', () => {
  it('does not fetch until Play, then uses authenticated Blob audio and revokes it', async () => {
    const requestBinary = vi.fn(async () => httpBinaryResponse('audio-bytes', {
      contentType: 'audio/mpeg',
    }))
    const http = httpTransportTestDouble({ requestBinary })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-1')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard(http)

    expect(requestBinary).not.toHaveBeenCalled()
    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()

    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/audio-1', {
      sessionKey: 'agent:main:webchat:ok',
      signal: expect.any(AbortSignal),
      timeoutMs: 0,
    })
    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(el.querySelector<HTMLAudioElement>('.msg-audio-card__player')?.src).toContain('blob:audio-1')

    app.unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:audio-1')
  })

  it('rejects cross-origin audio instead of falling back to an unauthenticated fetch', async () => {
    const requestBinary = vi.fn()
    const http = httpTransportTestDouble({ requestBinary })
    const crossOrigin = {
      ...artifact,
      download_url: 'https://files.example.test/audio/answer.mp3?token=secret',
    }
    const { app, el } = await mountCard(http, vi.fn(), crossOrigin)

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(requestBinary).not.toHaveBeenCalled()
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('error')
    app.unmount()
  })

  it('offers Retry after a fetch failure', async () => {
    const requestBinary = vi.fn()
      .mockRejectedValueOnce(new HttpTransportError('http-status', 'missing', 404))
      .mockResolvedValueOnce(httpBinaryResponse('audio', { contentType: 'audio/mpeg' }))
    const http = httpTransportTestDouble({ requestBinary })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-retry')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { app, el } = await mountCard(http)

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('error')

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await settle()
    expect(requestBinary).toHaveBeenCalledTimes(2)
    expect(el.querySelector('.msg-audio-card')?.getAttribute('data-state')).toBe('ready')
    app.unmount()
  })

  it('falls back to Download when the browser rejects the codec', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(async () => httpBinaryResponse('audio', {
        contentType: 'audio/x-unknown',
      })),
    })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('')
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:unused')
    const { app, el, onDownload } = await mountCard(http)

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
    const http = httpTransportTestDouble({
      requestBinary: vi.fn((_url, options) => {
        requestSignal = options?.signal
        return new Promise<TestHttpBinaryResponse>(() => undefined)
      }),
    })
    const { app, el } = await mountCard(http)

    el.querySelector<HTMLButtonElement>('.msg-audio-card__action')?.click()
    await Promise.resolve()
    expect(requestSignal?.aborted).toBe(false)

    app.unmount()
    expect(requestSignal?.aborted).toBe(true)
  })

  it('revokes loaded audio when session context changes', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(async () => httpBinaryResponse('audio', {
        contentType: 'audio/mpeg',
      })),
    })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audio-session')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const sessionKey = ref('agent:main:webchat:one')
    const Root = defineComponent({
      setup: () => () => h(AudioArtifactCard, {
        artifact,
        sessionKey: sessionKey.value,
      }),
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(Root)
    app.use(i18n)
    app.provide(ARTIFACT_WORKBENCH_KEY, {
      content: createV4ArtifactContentAccess(http),
    } as ArtifactWorkbench)
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
