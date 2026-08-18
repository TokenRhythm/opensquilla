// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { useRpcStore } from '@/stores/rpc'
import type { ArtifactPayload } from '@/types/rpc'
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
  const rpc = useRpcStore(pinia)
  rpc.auth = { principal: { isOwner: options.isOwner } }
  const app = createApp(ChatArtifactList, {
    artifacts: [options.artifact || htmlArtifact],
    sessionKey: 'agent:main:webchat:ok',
    authToken: 'secret',
    preferWorkbench: options.preferWorkbench,
    onDownload: options.onDownload,
    onOpen: options.onOpen,
  })
  app.use(pinia)
  app.use(i18n)
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
  platformState.id = 'web'
  platformState.capabilities.isDesktop = false
  platformState.capabilities.canOpenArtifactsNatively = false
  platformState.files.openArtifact.mockReset()
  const { dismissToast, toasts } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
})

describe('ChatArtifactList native HTML open', () => {
  it('posts HTML artifacts to the gateway native-open endpoint for owner Web sessions', async () => {
    const fetchImpl = vi.fn(async () => new Response('{"ok":true}', { status: 202 }))
    vi.stubGlobal('fetch', fetchImpl)
    const { app, el } = await mountList({ isOwner: true })

    const open = Array.from(el.querySelectorAll<HTMLButtonElement>('.msg-artifact-action'))
      .find(button => button.textContent?.includes('Open'))
    expect(open).toBeTruthy()
    open?.click()
    await settle()

    expect(fetchImpl).toHaveBeenCalledWith('/api/v1/artifacts/art-html/open', {
      method: 'POST',
      headers: {
        'x-opensquilla-session-key': 'agent:main:webchat:ok',
        Authorization: 'Bearer secret',
      },
      credentials: 'same-origin',
    })
    app.unmount()
  })

  it('renders HTML artifacts as download-only for non-owner Web sessions', async () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const onDownload = vi.fn()
    const { app, el } = await mountList({ isOwner: false, onDownload })

    expect(el.textContent).not.toContain('Open')
    expect(el.textContent).toContain('Download')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onDownload).toHaveBeenCalledWith(htmlArtifact)
    expect(fetchImpl).not.toHaveBeenCalled()
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
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<p>fixture</p>', {
      status: 200,
      headers: { 'content-type': 'text/html' },
    })))
    const { app, el } = await mountList({ isOwner: true })

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
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const onOpen = vi.fn()
    const { app, el } = await mountList({
      isOwner: false,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.textContent).toContain('Open')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onOpen).toHaveBeenCalledWith(htmlArtifact)
    expect(fetchImpl).not.toHaveBeenCalled()
    app.unmount()
  })

  it('routes Office files to the Workbench download-only document panel', async () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const onOpen = vi.fn()
    const officeArtifact: ArtifactPayload = {
      id: 'art-office',
      name: 'deck.pptx',
      mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      download_url: '/api/v1/artifacts/art-office',
    }
    const { app, el } = await mountList({
      isOwner: false,
      artifact: officeArtifact,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.textContent).toContain('Open')
    el.querySelector<HTMLButtonElement>('.msg-artifact-body')?.click()
    await nextTick()

    expect(onOpen).toHaveBeenCalledWith(officeArtifact)
    expect(fetchImpl).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps video in the transcript player even when Workbench routing is preferred', async () => {
    const fetchImpl = vi.fn(async () => new Response('video', {
      status: 200,
      headers: { 'content-type': 'video/webm' },
    }))
    vi.stubGlobal('fetch', fetchImpl)
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
      isOwner: false,
      artifact: videoArtifact,
      preferWorkbench: true,
      onOpen,
    })

    expect(el.querySelectorAll('.msg-video-card')).toHaveLength(1)
    expect(el.querySelectorAll('.msg-artifact-chip')).toHaveLength(0)
    expect(fetchImpl).not.toHaveBeenCalled()

    el.querySelector<HTMLButtonElement>('.msg-video-card__action')?.click()
    await settle()
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()

    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(el.querySelector('.msg-video-card__player')).toBeTruthy()
    expect(onOpen).not.toHaveBeenCalled()
    app.unmount()
  })
})
