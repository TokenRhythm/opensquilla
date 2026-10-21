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
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import { useWorkbenchResourcesStore } from '@/stores/workbenchResources'
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
  // The 600px rectangle above describes the native slot, not its workspace.
  // Scope isolation is exercised in split view; narrow visibility is explicit.
  const props = reactive({ routeActive: true, sessionId: 'session-a', availableWidth: 1200 })
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
  return { api, element, emit, pages, props, store, switchSession, visibleLayers,
    documents: useArtifactDocumentsStore(pinia), resources: useWorkbenchResourcesStore(pinia) }
}

describe('AppWorkbench retained browser session isolation', () => {
  it('reopens a closed browser from the new tab without replacing the blank tab', async () => {
    const harness = await mountWorkbench()
    harness.store.closeItem('page-a')
    harness.store.openItem(createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'about:blank' })!)
    await settle()
    const blankId = harness.store.activeItemId!
    const layer = harness.element.querySelector<HTMLElement>(`[data-workbench-item-id="${blankId}"]`)!
    const reopen = [...layer.querySelectorAll<HTMLButtonElement>('.browser-start button')]
      .find(button => button.textContent?.includes(en.workbench.browser.reopen))!
    expect(reopen).toBeDefined()
    reopen.click()
    await settle()
    expect(harness.store.activeItemId).toBe('page-a')
    expect(harness.store.visibleItems.map(item => item.id)).toEqual([blankId, 'page-a'])
    expect(harness.pages.get('page-a')?.visible).toBe(true)
  })

  it('retries blank surface creation and failed navigation within the same browser tab', async () => {
    const harness = await mountWorkbench()
    vi.mocked(harness.api.createSurface).mockResolvedValueOnce({
      ok: false, code: 'BROWSER_CRASHED', message: 'Synthetic browser creation failure',
    })
    harness.store.openItem(createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'about:blank' })!)
    await settle()
    const blankId = harness.store.activeItemId!
    const layer = harness.element.querySelector<HTMLElement>(`[data-workbench-item-id="${blankId}"]`)!
    expect(layer.querySelector('.browser-preview__error')?.textContent).toContain('Synthetic browser creation failure')
    expect(layer.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    layer.querySelector<HTMLButtonElement>('.browser-preview__error button')!.click()
    await settle()
    const start = layer.querySelector<HTMLElement>('[data-testid="browser-start"]')!
    expect(start).not.toBeNull()
    expect(harness.pages.get(blankId)?.visible).toBe(false)
    vi.mocked(harness.api.createSurface).mockClear()
    vi.mocked(harness.api.navigateSurface!).mockResolvedValueOnce({
      ok: false, code: 'NAVIGATION_FAILED', navigationError: {
        code: 'NAVIGATION_FAILED', message: 'Synthetic navigation failure', url: 'https://example.test/unavailable',
      },
    })
    const address = start.querySelector<HTMLInputElement>('input')!
    address.value = 'https://example.test/unavailable'
    address.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    start.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await settle()
    expect(harness.api.navigateSurface).toHaveBeenLastCalledWith({
      version: 2, surfaceId: blankId, action: 'navigate', url: 'https://example.test/unavailable',
    })
    expect(layer.querySelector('.browser-preview__error')?.textContent).toContain('Synthetic navigation failure')
    expect(harness.store.activeItemId).toBe(blankId)
    layer.querySelector<HTMLButtonElement>('.browser-preview__error button')!.click()
    await settle()
    expect(harness.api.navigateSurface).toHaveBeenLastCalledWith({
      version: 2, surfaceId: blankId, action: 'reload',
    })
    await harness.emit({ version: 2, surfaceId: blankId, type: 'navigation-state', detail: {
      sessionKey: 'session-a', url: 'https://example.test/recovered', loading: false,
    } })
    expect(layer.querySelector('.browser-preview__error')).toBeNull()
    expect(layer.querySelector('[data-workbench-native-surface-slot]')).not.toBeNull()
    expect(harness.pages.get(blankId)?.visible).toBe(true)
    expect(harness.store.activeItemId).toBe(blankId)
    expect(harness.api.createSurface).not.toHaveBeenCalled()
  })

  it('does not focus a blank start form opened in a background session', async () => {
    const harness = await mountWorkbench()
    const address = harness.element.querySelector<HTMLInputElement>('.browser-preview__address')!
    address.focus()
    harness.pages.set('background-blank', { url: 'about:blank', form: '', loggedIn: false, visible: false })
    await harness.emit({ version: 2, surfaceId: 'background-blank', type: 'browser-opened', detail: {
      sessionKey: 'session-b', url: 'about:blank', targetRef: 'background-target',
    } })
    expect(harness.store.activeItemId).toBe('page-a')
    expect(document.activeElement).toBe(address)
    expect(harness.pages.get('background-blank')?.visible).toBe(false)
  })

  it('retains the browser start page as its own tab and navigates that tab after switching sessions', async () => {
    const harness = await mountWorkbench()
    harness.element.querySelector<HTMLButtonElement>('[data-testid="workbench-new-panel-trigger"]')!.click()
    await settle()
    document.querySelector<HTMLButtonElement>('[data-testid="workbench-new-panel-menu-list"] [data-panel-kind="browser"]')!.click()
    await settle()

    const blank = harness.store.activeItem
    expect(blank).toMatchObject({
      kind: 'browser', title: 'New tab', scope: { type: 'session', id: 'session-a' },
      payload: { initialUrl: 'about:blank' },
    })
    expect(harness.store.visibleItems).toHaveLength(2)
    expect([...harness.element.querySelectorAll('[role="tab"]')].map(tab => tab.textContent))
      .toContain('New tab')
    const layer = harness.element.querySelector<HTMLElement>(`[data-workbench-item-id="${blank!.id}"]`)!
    const start = layer.querySelector<HTMLElement>('[data-testid="browser-start"]')!
    const address = start.querySelector<HTMLInputElement>('input')!
    expect(layer.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    expect(harness.pages.get(blank!.id)?.visible).toBe(false)
    address.value = 'example.test/new-page'
    address.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()

    harness.store.activateItem('page-a')
    await settle()
    expect(harness.visibleLayers()).toEqual(['page-a'])
    harness.store.activateItem(blank!.id)
    await settle()
    expect(harness.visibleLayers()).toEqual([blank!.id])
    expect(address.value).toBe('example.test/new-page')
    await harness.switchSession('session-b')
    expect(harness.visibleLayers()).toEqual([])
    harness.store.openItem(browser('page-b', 'session-b'))
    await settle()
    await harness.switchSession('session-a')
    expect(harness.visibleLayers()).toEqual([blank!.id])
    expect(layer.querySelector('[data-testid="browser-start"]')).toBe(start)
    expect(address.value).toBe('example.test/new-page')

    vi.mocked(harness.api.createSurface).mockClear()
    start.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await settle()
    expect(harness.api.navigateSurface).toHaveBeenCalledExactlyOnceWith({
      version: 2, surfaceId: blank!.id, action: 'navigate', url: 'https://example.test/new-page',
    })
    await harness.emit({ version: 2, surfaceId: blank!.id, type: 'navigation-state', detail: {
      sessionKey: 'session-a', url: 'https://example.test/new-page', loading: false,
    } })
    expect(harness.store.activeItem).toMatchObject({
      id: blank!.id, title: 'example.test', payload: { initialUrl: 'https://example.test/new-page' },
    })
    expect(harness.store.visibleItems).toHaveLength(2)
    expect(layer.querySelector('[data-testid="browser-start"]')).toBeNull()
    expect(layer.querySelector('[data-workbench-native-surface-slot]')).not.toBeNull()
    expect(harness.pages.get(blank!.id)?.visible).toBe(true)
    expect(harness.api.createSurface).not.toHaveBeenCalled()
  })

  it('adopts an agent-opened blank target, navigates it from the address bar and closes it', async () => {
    const harness = await mountWorkbench()
    vi.mocked(harness.api.createSurface).mockClear()
    harness.pages.set('blank-target', { url: 'about:blank', form: '', loggedIn: false, visible: false })
    await harness.emit({ version: 2, surfaceId: 'blank-target', type: 'browser-opened', detail: {
      sessionKey: 'session-a', url: 'about:blank', title: 'about:blank', targetRef: 'page-blank',
    } })

    expect(harness.store.activeItem).toMatchObject({ id: 'blank-target', title: 'New tab',
      payload: { initialUrl: 'about:blank', adoptedNativeSurface: true, targetRef: 'page-blank' } })
    expect(harness.visibleLayers()).toEqual(['blank-target'])
    expect(harness.pages.get('blank-target')?.visible).toBe(false)
    expect(harness.api.createSurface).not.toHaveBeenCalled()
    const panel = harness.element.querySelector<HTMLElement>('[data-workbench-item-id="blank-target"]')!
    const address = panel.querySelector<HTMLInputElement>('.browser-preview__address')!
    expect(panel.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    expect(panel.querySelector('[data-testid="browser-start"]')).not.toBeNull()
    expect(address.value).toBe('')
    expect(address.placeholder).toBe('Enter a URL, e.g. example.com')
    expect(panel.querySelector<HTMLButtonElement>('button[aria-label="Open externally"]')?.disabled).toBe(true)

    address.value = 'example.test/next'
    address.dispatchEvent(new Event('input', { bubbles: true }))
    panel.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await settle()
    expect(harness.api.navigateSurface).toHaveBeenCalledWith({ version: 2,
      surfaceId: 'blank-target', action: 'navigate', url: 'https://example.test/next' })
    await harness.emit({ version: 2, surfaceId: 'blank-target', type: 'navigation-state', detail: {
      sessionKey: 'session-a', url: 'https://example.test/next', targetRef: 'page-blank', loading: false,
    } })
    expect(address.value).toBe('https://example.test/next')
    expect(panel.querySelector('[data-workbench-native-surface-slot]')).not.toBeNull()
    expect(panel.querySelector('[data-testid="browser-start"]')).toBeNull()
    expect(harness.store.activeItem).toMatchObject({ id: 'blank-target', title: 'example.test',
      payload: { initialUrl: 'https://example.test/next', targetRef: 'page-blank' } })
    expect(harness.api.createSurface).not.toHaveBeenCalled()

    await harness.emit({ version: 2, surfaceId: 'blank-target', type: 'browser-closed', detail: {
      sessionKey: 'session-a', targetRef: 'page-blank',
    } })
    expect(harness.store.items.some(item => item.id === 'blank-target')).toBe(false)
    expect(harness.api.destroySurface).toHaveBeenCalledWith('blank-target')
  })

  it('hides a narrow native page without closing or recreating its retained session', async () => {
    const harness = await mountWorkbench()
    const page = harness.pages.get('page-a')!
    page.form = 'synthetic retained input'
    page.loggedIn = true
    const lifecycle = vi.fn()
    const unsubscribe = harness.store.onLifecycle(lifecycle)
    expect(page.visible).toBe(true)

    harness.props.availableWidth = 600
    await settle()
    expect(page.visible).toBe(false)
    expect(harness.store.expanded).toBe(true)
    expect(harness.store.hostAvailable).toBe(true)
    expect(harness.pages.get('page-a')).toBe(page)
    expect(harness.api.navigateSurface).not.toHaveBeenCalled()
    expect(harness.api.destroySurface).not.toHaveBeenCalled()

    harness.props.availableWidth = 1200
    await settle()
    expect(harness.pages.get('page-a')).toBe(page)
    expect(page).toMatchObject({ form: 'synthetic retained input', loggedIn: true, visible: true })
    expect(harness.api.createSurface).toHaveBeenCalledOnce()
    expect(lifecycle).not.toHaveBeenCalled()
    unsubscribe()
  })

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
    // Background popups are retained without replacing the user's selection.
    expect(harness.visibleLayers()).toEqual(['page-a'])
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

  it('switches immediately without close guards or disposing the old owner', async () => {
    const harness = await mountWorkbench()
    const clearDocuments = vi.spyOn(harness.documents, 'clearSession')
    const clearResources = vi.spyOn(harness.resources, 'clearSession')
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
    expect(beforeClose).not.toHaveBeenCalled()
    expect(clearDocuments).not.toHaveBeenCalled()
    expect(clearResources).not.toHaveBeenCalled()
    expect(harness.store.activeSessionId).toBe('session-b')
    expect(harness.element.querySelector<HTMLElement>('[data-testid="workbench-host"]')?.style.display)
      .toBe('none')
    expect(harness.pages.get('page-a')?.visible).toBe(false)
    await harness.emit({ version: 2, surfaceId: 'popup-b', type: 'browser-opened', detail: {
      sessionKey: 'session-b', url: 'https://example.test/popup-b', targetRef: 'target-popup-b',
    } })
    expect(harness.store.activeItemId).toBe('popup-b')

    await harness.switchSession('session-c')
    finishSave(true)
    await settle()
    expect(harness.store.activeSessionId).toBe('session-c')
    expect(harness.store.activeItemId).toBeNull()
    expect(harness.visibleLayers()).toEqual([])
    expect(harness.store.items.map(item => item.id)).toEqual(['page-a', 'saving-panel', 'popup-b'])
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
