// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceResult,
  Platform,
} from '@/platform/types'
import en from '@/locales/en.json'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import type { WorkbenchComponentEvent } from '@/workbench/types'
import BrowserPreviewPanel from './BrowserPreviewPanel.vue'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'

const sourceUrl = 'https://example.test/source'
const downloadUrl = 'https://example.test/attachment.pdf'
const cleanups: Array<() => Promise<void>> = []

afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) await cleanup()
  vi.unstubAllGlobals()
})

async function mountBrowser() {
  const requests: Array<{
    promise: Promise<NativeWorkbenchSurfaceResult>
    resolve(result: NativeWorkbenchSurfaceResult): void
  }> = []
  const actions: Array<Promise<void>> = []
  const navigateSurface = vi.fn(() => {
    let resolve!: (result: NativeWorkbenchSurfaceResult) => void
    const promise = new Promise<NativeWorkbenchSurfaceResult>(done => { resolve = done })
    requests.push({ promise, resolve })
    return promise
  })
  const api: NativeWorkbenchApi = {
    getCapabilities: async () => ({ protocolVersions: [2], modes: ['full'] }),
    createSurface: async () => ({ ok: true }),
    setSurfaceRect: async () => ({ ok: true }),
    activateSurface: async () => ({ ok: true }),
    destroySurface: vi.fn(async () => ({ ok: true })),
    navigateSurface,
    onSurfaceEvent: () => () => undefined,
  }
  const item = createBrowserWorkbenchItem({ scopeId: 'session-address', url: sourceUrl })!
  const state = reactive<Record<string, unknown>>({})
  const definition = createBrowserWorkbenchDefinition({
    confirmPermission: async () => false,
    openExternal: vi.fn(),
    platform: {} as Platform,
    t: key => key,
  })
  const runtime = await definition.createRuntime!(item, {
    nativeWorkbenchApi: api,
    getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true,
    reportError: vi.fn(),
    setExpanded: vi.fn(),
  })
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(BrowserPreviewPanel, {
    ...definition.getProps!(item, { runtimeState: state, active: true,
      hostAvailable: true, nativeSurface: true }),
    'onWorkbench-event': (event: WorkbenchComponentEvent) => {
      actions.push(Promise.resolve(runtime.handleComponentEvent!(event, item)))
    },
  }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  cleanups.push(async () => {
    app.unmount()
    element.remove()
    await runtime.dispose?.('closed')
  })
  const address = element.querySelector<HTMLInputElement>('.browser-preview__address')!
  const editNow = (value: string) => {
    address.value = value
    address.dispatchEvent(new Event('input', { bubbles: true }))
  }
  const edit = async (value: string) => { editNow(value); await nextTick() }
  const submit = () => element.querySelector<HTMLFormElement>('form')!.dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  )
  const emit = async (type: NativeWorkbenchSurfaceEvent['type'],
    detail: NativeWorkbenchSurfaceEvent['detail'] = {}) => {
    await runtime.handleNativeSurfaceEvent!({ version: 2, surfaceId: item.id, type, detail }, item)
    await nextTick()
  }
  const ready = (url = sourceUrl) => emit('navigation-state', {
    url, loading: false, pageState: 'ready', navigationError: null,
  })
  const settle = async (index: number, result: NativeWorkbenchSurfaceResult = { ok: true }) => {
    requests[index]!.resolve(result)
    await actions[index]
    await nextTick()
  }
  await ready()
  return { address, state, api, requests, actions, edit, editNow, submit, emit, ready, settle,
    runtime, item }
}

it.each(['completed', 'cancelled'] as const)(
  'restores the source address after a submitted attachment Save is %s', async downloadState => {
    const browser = await mountBrowser()
    await browser.edit(downloadUrl)
    browser.submit()
    expect(browser.api.navigateSurface).toHaveBeenCalledExactlyOnceWith({
      version: 2, surfaceId: browser.item.id, action: 'navigate', url: downloadUrl,
    })
    const downloadId = 'download-12345678-1234-1234-1234-123456789abc'
    await browser.emit('download-state', { downloadId, downloadState: 'progressing',
      downloadName: 'synthetic.pdf' })
    await browser.ready()
    await browser.emit('ready')
    await browser.emit('download-state', { downloadId, downloadState,
      downloadName: 'synthetic.pdf' })
    await browser.settle(0)
    expect(browser.address.value).toBe(sourceUrl)
    expect(browser.state).toMatchObject({ currentUrl: sourceUrl, loading: false, errorMessage: '' })
    expect(browser.api.destroySurface).not.toHaveBeenCalled()
  },
)

it('restores the source address after beforeunload cancels the submitted navigation', async () => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.ready()
  await browser.settle(0, { ok: false, code: 'NAVIGATION_CANCELLED' })
  expect(browser.address.value).toBe(sourceUrl)
  expect(browser.state.errorMessage).toBe('')
})

it('keeps an unsubmitted address draft through readiness and an unrelated URL change', async () => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  await browser.ready()
  await browser.emit('ready')
  await browser.ready('https://example.test/elsewhere')
  expect(browser.address.value).toBe(downloadUrl)
  expect(browser.api.navigateSurface).not.toHaveBeenCalled()
})

it.each([true, false])('keeps a newer draft when an earlier navigation settles (ok=%s)', async ok => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.edit('https://example.test/new-draft')
  await browser.ready('https://example.test/redirect')
  await browser.settle(0, ok ? { ok: true } : { ok: false, code: 'NAVIGATION_CANCELLED' })
  expect(browser.address.value).toBe('https://example.test/new-draft')
})

it('keeps a new draft edited away and back to the submitted text', async () => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.edit('https://example.test/new-draft')
  await browser.edit(downloadUrl)
  await browser.ready()
  await browser.settle(0)
  expect(browser.address.value).toBe(downloadUrl)
})

it.each([downloadUrl, 'https://example.test/second.pdf'])(
  'does not apply an older request result to the latest submitted address: %s', async secondUrl => {
    const browser = await mountBrowser()
    await browser.edit(downloadUrl)
    browser.submit()
    await browser.edit(secondUrl)
    browser.submit()
    await browser.settle(0)
    expect(browser.address.value).toBe(secondUrl)
    await browser.ready()
    await browser.settle(1)
    expect(browser.address.value).toBe(sourceUrl)
  },
)

it('retains the latest result when overlapping requests finish out of order in one Vue update', async () => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.edit('https://example.test/second.pdf')
  browser.submit()
  browser.requests[1]!.resolve({ ok: true })
  browser.requests[0]!.resolve({ ok: false, code: 'NAVIGATION_CANCELLED' })
  await Promise.all(browser.actions)
  await nextTick()
  expect(browser.address.value).toBe(sourceUrl)
})

it.each([downloadUrl, 'https://example.test/second.pdf'])(
  'matches a new submission during the same Vue update as an earlier result: %s', async secondUrl => {
    const browser = await mountBrowser()
    await browser.edit(downloadUrl)
    browser.submit()
    const nextSubmit = browser.requests[0]!.promise.then(() => {
      browser.editNow(secondUrl)
      browser.submit()
    })
    await browser.settle(0)
    await nextSubmit
    await nextTick()
    expect(browser.requests).toHaveLength(2)
    expect(browser.address.value).toBe(secondUrl)
    await browser.settle(1)
    expect(browser.address.value).toBe(sourceUrl)
  },
)

it('uses the committed redirect address when the submitted text has no newer edits', async () => {
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.ready('https://example.test/redirect')
  await browser.settle(0)
  expect(browser.address.value).toBe('https://example.test/redirect')
})

it('settles an address submission when Web Crypto randomUUID is unavailable', async () => {
  vi.stubGlobal('crypto', {})
  const browser = await mountBrowser()
  await browser.edit(downloadUrl)
  browser.submit()
  await browser.settle(0)
  expect(browser.address.value).toBe(sourceUrl)
})

it.each(['navigate', 'back', 'reload'] as const)(
  'updates the committed address for %s events without an address submission ID', async action => {
    const browser = await mountBrowser()
    const pending = Promise.resolve(browser.runtime.handleComponentEvent!({ type: 'browser-action',
      payload: { action, ...(action === 'navigate' ? { url: downloadUrl } : {}) } }, browser.item))
    await browser.ready('https://example.test/elsewhere')
    browser.requests[0]!.resolve({ ok: true })
    await pending
    await nextTick()
    expect(browser.address.value).toBe('https://example.test/elsewhere')
  },
)
