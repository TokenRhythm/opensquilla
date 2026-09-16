// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { ArtifactPayload } from '@/types/artifacts'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import {
  httpBinaryResponse,
  httpTransportTestDouble,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import ChatArtifactList from './ChatArtifactList.vue'

const platformState = vi.hoisted(() => ({
  id: 'web' as 'web' | 'desktop',
  capabilities: {
    isDesktop: false,
    canOpenArtifactsNatively: false,
  },
  files: {
    openArtifact: vi.fn(),
  },
}))

vi.mock('@/platform', () => ({
  usePlatform: () => platformState,
}))

const htmlArtifact: ArtifactPayload = {
  id: 'art-html',
  name: 'page.html',
  mime: 'text/html',
  download_url: '/api/v1/artifacts/art-html',
}

async function settle() {
  await Promise.resolve()
  await nextTick()
}

async function mountList(options: {
  http: TestHttpTransport
  isOwner: boolean
  artifact?: ArtifactPayload
  preferWorkbench?: boolean
  onDownload?: (artifact: ArtifactPayload) => void
  onOpen?: (artifact: ArtifactPayload) => void
}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const pinia = createPinia()
  setActivePinia(pinia)
  const app = createApp(ChatArtifactList, {
    artifacts: [options.artifact || htmlArtifact],
    sessionKey: 'agent:main:webchat:ok',
    preferWorkbench: options.preferWorkbench,
    onDownload: options.onDownload,
    onOpen: options.onOpen,
  })
  app.use(pinia)
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, {
    isLocalOwner: options.isOwner,
  } as GatewayAccess)
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    content: createV4ArtifactContentAccess(options.http),
    previews: createV4ArtifactPreviews(options.http, { baseOrigin: () => 'http://localhost' }),
  } as ArtifactWorkbench)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.stubGlobal('sessionStorage', {
    getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? 'secret' : null),
  })
  platformState.id = 'web'
  platformState.capabilities.isDesktop = false
  platformState.capabilities.canOpenArtifactsNatively = false
  platformState.files.openArtifact.mockReset()
  const { dismissToast, toasts } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
})

describe('ChatArtifactList native HTML open', () => {
  it('posts HTML artifacts to the gateway native-open endpoint for owner Web sessions', async () => {
    const requestBinary = vi.fn(async () => httpBinaryResponse('{"ok":true}', { status: 202 }))
    const http = httpTransportTestDouble({ requestBinary })
    const { app, el } = await mountList({ http, isOwner: true })

    const open = Array.from(el.querySelectorAll<HTMLButtonElement>('.msg-artifact-action'))
      .find(button => button.textContent?.includes('Open'))
    expect(open).toBeTruthy()
    open?.click()
    await settle()

    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/art-html/open', {
      method: 'POST',
      sessionKey: 'agent:main:webchat:ok',
      timeoutMs: 0,
    })
    app.unmount()
  })

  it('renders HTML artifacts as download-only for non-owner Web sessions', async () => {
    const requestBinary = vi.fn()
    const http = httpTransportTestDouble({ requestBinary })
    const onDownload = vi.fn()
    const { app, el } = await mountList({ http, isOwner: false, onDownload })

    expect(el.textContent).not.toContain('Open')
    expect(el.textContent).toContain('Download')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onDownload).toHaveBeenCalledWith(htmlArtifact)
    expect(requestBinary).not.toHaveBeenCalled()
    app.unmount()
  })

  it('does not expose a Desktop native-open diagnostic in the toast', async () => {
    const diagnostic = 'spawn ENOENT /fixture/private/page.html'
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    platformState.id = 'desktop'
    platformState.capabilities.isDesktop = true
    platformState.capabilities.canOpenArtifactsNatively = true
    platformState.files.openArtifact.mockResolvedValue({
      ok: false,
      message: diagnostic,
    })
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(async () => httpBinaryResponse('<p>fixture</p>', {
        contentType: 'text/html',
      })),
    })
    const { app, el } = await mountList({ http, isOwner: true })

    const open = Array.from(el.querySelectorAll<HTMLButtonElement>('.msg-artifact-action'))
      .find(button => button.textContent?.includes('Open'))
    expect(open).toBeTruthy()
    open?.click()
    await settle()

    await vi.waitFor(() => {
      expect(platformState.files.openArtifact).toHaveBeenCalledOnce()
    })
    const toastItems = useToasts().toasts.value
    const latestToast = toastItems[toastItems.length - 1]
    expect(latestToast?.message).toBe(i18n.global.t('chat.toast.artifactOpenFailed'))
    expect(latestToast?.message).not.toContain(diagnostic)
    expect(warn).toHaveBeenCalledWith('[artifact] Native open failed:', diagnostic)
    app.unmount()
  })

  it('routes previewable artifacts to the Workbench without fetching or opening a popup', async () => {
    const requestBinary = vi.fn()
    const http = httpTransportTestDouble({ requestBinary })
    const onOpen = vi.fn()
    const { app, el } = await mountList({
      http,
      isOwner: false,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.textContent).toContain('Open')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onOpen).toHaveBeenCalledWith(htmlArtifact)
    expect(requestBinary).not.toHaveBeenCalled()
    app.unmount()
  })

  it('routes Office files to the Workbench download-only document panel', async () => {
    const requestBinary = vi.fn()
    const http = httpTransportTestDouble({ requestBinary })
    const onOpen = vi.fn()
    const officeArtifact: ArtifactPayload = {
      id: 'art-office',
      name: 'deck.pptx',
      mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      download_url: '/api/v1/artifacts/art-office',
    }
    const { app, el } = await mountList({
      http,
      isOwner: false,
      artifact: officeArtifact,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.textContent).toContain('Open')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onOpen).toHaveBeenCalledWith(officeArtifact)
    expect(requestBinary).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps video in the transcript player even when Workbench routing is preferred', async () => {
    const requestBinary = vi.fn(async () => httpBinaryResponse('video', {
      contentType: 'video/webm',
    }))
    const http = httpTransportTestDouble({ requestBinary })
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:inline-video')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const onOpen = vi.fn()
    const videoArtifact: ArtifactPayload = {
      id: 'art-video',
      name: 'clip.webm',
      mime: 'video/webm',
      download_url: '/api/v1/artifacts/art-video',
    }
    const { app, el } = await mountList({
      http,
      isOwner: false,
      artifact: videoArtifact,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.querySelectorAll('.msg-video-card')).toHaveLength(1)
    expect(el.querySelectorAll('.msg-artifact-chip')).toHaveLength(0)
    expect(requestBinary).not.toHaveBeenCalled()

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()

    expect(requestBinary).toHaveBeenCalledOnce()
    expect(el.querySelector('.msg-video-card__player')).toBeTruthy()
    expect(onOpen).not.toHaveBeenCalled()
    app.unmount()
  })
})
