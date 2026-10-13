// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import type { NativeWorkbenchApi, NativeWorkbenchCapabilities,
  NativeWorkbenchSurfaceEvent, Platform } from '@/platform/types'
import { useConfirm } from '@/composables/useConfirm'
import { useWorkbenchStore } from '@/workbench/store'
import { workbenchPanelRegistry } from '@/workbench/registry'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import en from '@/locales/en.json'
import AppWorkbench from './AppWorkbench.vue'

const platform = vi.hoisted(() => ({ current: null as Platform | null }))
vi.mock('@/platform', () => ({ usePlatform: () => platform.current }))

const cleanups: Array<() => void> = []

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
    x: 600, y: 50, top: 50, right: 1200, bottom: 650, left: 600,
    width: 600, height: 600, toJSON: () => ({}),
  }))
})

afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  await settle()
  useConfirm().resolveConfirm(false)
  workbenchPanelRegistry.clear()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

async function settle() {
  for (let index = 0; index < 6; index += 1) {
    await nextTick()
    await vi.advanceTimersByTimeAsync(20)
  }
}

function browser(id: string, session: string) {
  const item = createBrowserWorkbenchItem({ scopeId: session,
    url: `https://example.test/${session}` })!
  return { ...item, id }
}

async function mountWorkbench() {
  const pages = new Map<string, { url: string; form: string; loggedIn: boolean; visible: boolean }>()
  let surfaceEvent: ((event: NativeWorkbenchSurfaceEvent) => void) | undefined
  const api: NativeWorkbenchApi = {
    getCapabilities: vi.fn(async (): Promise<NativeWorkbenchCapabilities> => ({ protocolVersions: [2], modes: ['full', 'offline'],
      navigationActions: ['close'] })),
    createSurface: vi.fn(async request => {
      pages.set(request.surfaceId, { url: String(request.payload.url),
        form: '', loggedIn: false, visible: false })
      return { ok: true }
    }),
    setSurfaceRect: vi.fn(async request => {
      const page = pages.get(request.surfaceId)
      if (page) page.visible = request.visible
      return { ok: true }
    }),
    activateSurface: vi.fn(async () => ({ ok: true })),
    destroySurface: vi.fn(async id => { pages.delete(id); return { ok: true } }),
    navigateSurface: vi.fn(async () => ({ ok: true })),
    respondToPermission: vi.fn(async () => ({ ok: true })),
    onSurfaceEvent: vi.fn(callback => {
      surfaceEvent = callback
      return () => { surfaceEvent = undefined }
    }),
  }
  platform.current = { workbench: { native: api } } as Platform
  const pinia = createPinia()
  const store = useWorkbenchStore(pinia)
  store.setSessionScope('session-a')
  store.openItem(browser('page-a', 'session-a'))
  const props = reactive({ routeActive: true, sessionId: 'session-a' })
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(AppWorkbench, props) })
  app.use(pinia)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    documents: {}, content: {}, previews: {}, resources: {},
    subscribeDocumentChanges: () => ({ close: vi.fn() }),
  } as unknown as ArtifactWorkbench)
  app.mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  await settle()
  const emit = async (event: NativeWorkbenchSurfaceEvent) => {
    if (!surfaceEvent) throw new Error('Native surface event listener was not attached')
    surfaceEvent(event)
    await settle()
  }
  const switchSession = async (sessionId: string) => {
    props.sessionId = sessionId
    await settle()
  }
  const visibleLayers = () => [...element.querySelectorAll<HTMLElement>('[data-workbench-item-id]')]
    .filter(layer => layer.style.display !== 'none').map(layer => layer.dataset.workbenchItemId)
  return { api, element, emit, pages, props, store, switchSession, visibleLayers }
}

describe('AppWorkbench retained browser session isolation', () => {
  it('switches through the mounted scope watcher without closing or recreating dirty pages', async () => {
    const harness = await mountWorkbench()
    const pageA = harness.pages.get('page-a')!
    pageA.form = 'synthetic unsaved input'
    pageA.loggedIn = true
    expect(pageA.visible).toBe(true)

    await harness.switchSession('session-b')
    expect(harness.store.activeSessionId).toBe('session-b')
    expect(harness.store.activeItemId).toBeNull()
    expect(harness.visibleLayers()).toEqual([])
    expect(pageA.visible).toBe(false)
    expect(harness.api.navigateSurface).not.toHaveBeenCalled()
    expect(harness.api.destroySurface).not.toHaveBeenCalled()

    harness.store.openItem(browser('page-b', 'session-b'))
    await settle()
    const pageB = harness.pages.get('page-b')!
    pageB.form = 'different synthetic input'
    expect(harness.visibleLayers()).toEqual(['page-b'])
    expect(pageB.visible).toBe(true)

    await harness.switchSession('session-a')
    expect(harness.visibleLayers()).toEqual(['page-a'])
    expect(harness.pages.get('page-a')).toBe(pageA)
    expect(pageA).toMatchObject({ form: 'synthetic unsaved input', loggedIn: true, visible: true })
    expect(pageB.visible).toBe(false)
    expect(harness.api.createSurface).toHaveBeenCalledTimes(2)

    await harness.switchSession('session-b')
    expect(harness.pages.get('page-b')).toBe(pageB)
    expect(pageB).toMatchObject({ form: 'different synthetic input', visible: true })
    expect(harness.api.navigateSurface).not.toHaveBeenCalled()
    expect(harness.api.destroySurface).not.toHaveBeenCalled()
  })

  it('keeps background popup, navigation and Escape events inside their originating session', async () => {
    const harness = await mountWorkbench()
    await harness.switchSession('session-b')
    harness.store.openItem(browser('page-b', 'session-b'))
    await settle()
    harness.api.activateSurface = vi.fn(async () => ({ ok: true }))
    harness.pages.set('popup-a', { url: 'https://example.test/popup-a',
      form: '', loggedIn: true, visible: false })

    await harness.emit({ version: 2, surfaceId: 'popup-a', type: 'browser-opened', detail: {
      sessionKey: 'session-a', url: 'https://example.test/popup-a', targetRef: 'target-popup-a',
    } })
    await harness.emit({ version: 2, surfaceId: 'page-a', type: 'navigation-state', detail: {
      sessionKey: 'session-a', url: 'https://example.test/updated-a', loading: false,
    } })
    await harness.emit({ version: 2, surfaceId: 'page-a', type: 'escape' })
    expect(harness.store.activeItemId).toBe('page-b')
    expect(harness.store.expanded).toBe(true)
    expect(harness.visibleLayers()).toEqual(['page-b'])
    expect(harness.pages.get('popup-a')?.visible).toBe(false)
    expect(harness.api.activateSurface).not.toHaveBeenCalledWith('popup-a')
    expect(harness.element.querySelector<HTMLInputElement>(
      '[data-workbench-item-id="page-b"] .browser-preview__address')?.value)
      .toBe('https://example.test/session-b')

    await harness.switchSession('session-a')
    expect(harness.visibleLayers()).toEqual(['popup-a'])
    expect(harness.store.visibleItems.map(item => item.id)).toEqual(['page-a', 'popup-a'])
    expect(harness.api.createSurface).toHaveBeenCalledTimes(2)
  })

  it('rejects conflicting surface identities and denies background permission prompts', async () => {
    const harness = await mountWorkbench()
    await harness.switchSession('session-b')
    harness.store.openItem(browser('page-b', 'session-b'))
    await settle()
    await harness.emit({ version: 2, surfaceId: 'page-b', type: 'browser-opened', detail: {
      sessionKey: 'session-a', url: 'https://example.test/wrong-session', targetRef: 'wrong-target',
    } })
    await harness.emit({ version: 2, surfaceId: 'page-b', type: 'navigation-state', detail: {
      sessionKey: 'session-a', url: 'https://example.test/wrong-session',
    } })
    await harness.emit({ version: 2, surfaceId: 'page-a', type: 'permission-request', detail: {
      sessionKey: 'session-a', requestId: 'background-camera', permission: 'media',
      requestingOrigin: 'https://example.test',
    } })
    expect(harness.store.activeItemId).toBe('page-b')
    expect(harness.store.items.find(item => item.id === 'page-b')?.scope)
      .toEqual({ type: 'session', id: 'session-b' })
    expect(harness.store.items.find(item => item.id === 'page-b')?.payload.initialUrl)
      .toBe('https://example.test/session-b')
    expect(useConfirm().confirmState.value).toBeNull()
    expect(harness.api.respondToPermission).toHaveBeenCalledWith({ version: 2, surfaceId: 'page-a',
      requestId: 'background-camera', allow: false })
    expect(harness.visibleLayers()).toEqual(['page-b'])
  })

  it('hides the prior session while another panel saves and fences rapid scope changes', async () => {
    const harness = await mountWorkbench()
    let finishSave!: (accepted: boolean) => void
    const saving = new Promise<boolean>(resolve => { finishSave = resolve })
    const beforeClose = vi.fn(() => saving)
    workbenchPanelRegistry.register({ kind: 'terminal',
      createRuntime: () => ({ beforeClose }) }, { replace: true })
    harness.store.openItem({ id: 'saving-panel', kind: 'terminal', title: 'Synthetic pending panel',
      scope: { type: 'session', id: 'session-a' }, hostKind: 'dom',
      retention: 'keep-alive', payload: {} }, { activate: false })
    await settle()

    await harness.switchSession('session-b')
    expect(beforeClose).toHaveBeenCalledOnce()
    expect(harness.store.activeSessionId).toBe('session-a')
    expect(harness.element.querySelector<HTMLElement>('[data-testid="workbench-host"]')?.style.display)
      .toBe('none')
    expect(harness.pages.get('page-a')?.visible).toBe(false)
    await harness.emit({ version: 2, surfaceId: 'popup-b', type: 'browser-opened', detail: {
      sessionKey: 'session-b', url: 'https://example.test/popup-b', targetRef: 'target-popup-b',
    } })
    expect(harness.store.activeItemId).toBe('page-a')

    await harness.switchSession('session-c')
    finishSave(true)
    await settle()
    expect(harness.store.activeSessionId).toBe('session-c')
    expect(harness.store.activeItemId).toBeNull()
    expect(harness.visibleLayers()).toEqual([])
    expect(harness.store.items.map(item => item.id)).toEqual(['page-a', 'popup-b'])
    expect(harness.api.navigateSurface).not.toHaveBeenCalled()
    expect(harness.api.destroySurface).not.toHaveBeenCalled()

    await harness.switchSession('session-b')
    expect(harness.store.activeItemId).toBe('popup-b')
    expect(harness.visibleLayers()).toEqual(['popup-b'])
  })

  it('still asks the native close guard on explicit tab close and preserves Stay', async () => {
    const harness = await mountWorkbench()
    const navigate = vi.mocked(harness.api.navigateSurface!)
    navigate.mockResolvedValueOnce({ ok: false, code: 'CLOSE_CANCELLED' })
    const close = () => harness.element.querySelector<HTMLButtonElement>(
      '.workbench-host__tab-close')!.click()
    close()
    await settle()
    expect(navigate).toHaveBeenLastCalledWith({ version: 2, surfaceId: 'page-a', action: 'close' })
    expect(harness.store.items).toHaveLength(1)
    expect(harness.pages.get('page-a')?.visible).toBe(true)
    expect(harness.api.destroySurface).not.toHaveBeenCalled()
    close()
    await settle()
    expect(harness.store.items).toEqual([])
    expect(harness.api.destroySurface).toHaveBeenCalledWith('page-a')
  })
})
