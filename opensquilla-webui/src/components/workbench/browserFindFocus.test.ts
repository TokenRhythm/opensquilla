// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import type {
  NativeWorkbenchApi, NativeWorkbenchCapabilities, NativeWorkbenchNavigateRequest, Platform,
} from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import type { WorkbenchComponentEvent } from '@/workbench/types'
import BrowserPreviewPanel from './BrowserPreviewPanel.vue'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'

const cleanups: Array<() => void> = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.useRealTimers()
})

async function mountBrowser() {
  const state = reactive<Record<string, unknown>>({})
  const navigateSurface = vi.fn(async (_request: NativeWorkbenchNavigateRequest) => ({ ok: true as const }))
  const api: NativeWorkbenchApi = {
    getCapabilities: vi.fn(async (): Promise<NativeWorkbenchCapabilities> => ({
      protocolVersions: [1, 2], modes: ['full'],
    })),
    createSurface: vi.fn(async () => ({ ok: true })),
    setSurfaceRect: vi.fn(async () => ({ ok: true })),
    activateSurface: vi.fn(async () => ({ ok: true })),
    destroySurface: vi.fn(async () => ({ ok: true })),
    onSurfaceEvent: vi.fn(() => () => undefined),
    navigateSurface,
  }
  const item = createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'https://example.test/' })!
  const definition = createBrowserWorkbenchDefinition({
    platform: {} as Platform,
    confirmPermission: vi.fn(async () => false),
    openExternal: vi.fn(),
    t: key => key,
  })
  let active = true
  const runtime = await definition.createRuntime!(item, {
    nativeWorkbenchApi: api,
    getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true,
    isItemActive: () => active,
    setExpanded: vi.fn(),
    reportError: vi.fn(),
  })
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(BrowserPreviewPanel, {
    ...definition.getProps!(item, {
      active, hostAvailable: true, nativeSurface: true, runtimeState: state,
    }),
    'onWorkbench-event': (event: WorkbenchComponentEvent) => {
      void runtime.handleComponentEvent?.(event, item)
    },
  }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  const requestFind = async () => {
    await runtime.handleNativeSurfaceEvent?.({
      version: 2, surfaceId: item.id, type: 'find-requested',
    }, item)
    await nextTick()
    await nextTick()
  }
  return { element, item, navigateSurface, requestFind, suspend: () => { active = false } }
}

it('refocuses an already open find input on every native page request', async () => {
  vi.useFakeTimers()
  const { element, item, navigateSurface, requestFind } = await mountBrowser()
  const address = element.querySelector<HTMLInputElement>('.browser-preview__address')!
  await requestFind()
  const find = element.querySelector<HTMLInputElement>('.browser-preview__find-input')!
  expect(document.activeElement).toBe(find)
  for (const query of ['second request', 'third request']) {
    // The owner renderer restores the last DOM focus when the native page
    // sends its keyboard shortcut. The find request must explicitly replace it.
    address.focus()
    expect(document.activeElement).toBe(address)
    await requestFind()
    expect(document.activeElement).toBe(find)
    const focused = document.activeElement as HTMLInputElement
    focused.value = query
    focused.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(120)
    expect(navigateSurface).toHaveBeenLastCalledWith({
      version: 2, surfaceId: item.id, action: 'find', query,
    })
    expect(address.value).toBe('https://example.test/')
    expect(navigateSurface.mock.calls.every(([request]) => request.action === 'find')).toBe(true)
  }
})

it.each(['ctrlKey', 'metaKey'] as const)('refocuses open find on repeated parent %s shortcuts', async key => {
  const { element, requestFind } = await mountBrowser()
  await requestFind()
  const find = element.querySelector<HTMLInputElement>('.browser-preview__find-input')!
  const address = element.querySelector<HTMLInputElement>('.browser-preview__address')!
  address.focus()
  const event = new KeyboardEvent('keydown', {
    key: 'f', [key]: true, bubbles: true, cancelable: true,
  })
  address.dispatchEvent(event)
  await nextTick()
  await nextTick()
  expect(event.defaultPrevented).toBe(true)
  expect(document.activeElement).toBe(find)
})

it('ignores a native find request after its browser session is suspended', async () => {
  const { element, requestFind, suspend } = await mountBrowser()
  const address = element.querySelector<HTMLInputElement>('.browser-preview__address')!
  address.focus()
  suspend()
  await requestFind()
  expect(document.activeElement).toBe(address)
  expect(element.querySelector('.browser-preview__find-input')).toBeNull()
})
