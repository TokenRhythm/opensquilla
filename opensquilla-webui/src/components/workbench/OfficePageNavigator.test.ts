// @vitest-environment happy-dom
import { createApp, h, nextTick, shallowReactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OfficePageNavigator from './OfficePageNavigator.vue'
import type { OfficePageThumbnail, OfficePreviewSession } from '@/workbench/officePreviewAdapter'
import en from '@/locales/en.json'

const cleanups: (() => void)[] = []
let resized: ResizeObserverCallback
const created = vi.fn<(blob: Blob | MediaSource) => string>()
const revoked = vi.fn<(url: string) => void>()
let nextUrl = 0
function thumbnail(label = 'page'): OfficePageThumbnail { return { blob: new Blob([label], { type: 'image/png' }), width: 120, height: 160 } }
function session(render = vi.fn(async (_index: number, _signal?: AbortSignal) => thumbnail())): OfficePreviewSession {
  return { renderThumbnail: render, getPageSize: () => ({ width: 600, height: 800 }), getNotes: () => null,
    destroy: vi.fn(), clearSelection: vi.fn(), setSelectionMode: vi.fn(), goTo: vi.fn(), next: vi.fn(), previous: vi.fn(),
    fitPage: vi.fn(), fitWidth: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(), resize: vi.fn() }
}
async function settle() { for (let index = 0; index < 40; index++) { await Promise.resolve(); await nextTick() } }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(complete => { resolve = complete })
  return { promise, resolve }
}
async function mount(overrides: Partial<{ session: OfficePreviewSession | null; total: number; active: number; format: 'docx' | 'pptx'; collapsed: boolean }> = {}) {
  const host = document.createElement('div'); document.body.append(host)
  const state = shallowReactive({ session: session() as OfficePreviewSession | null, total: 1000, active: 0, format: 'docx' as 'docx' | 'pptx', collapsed: false, ...overrides })
  const navigate = vi.fn(), close = vi.fn(), expand = vi.fn()
  const app = createApp({ render: () => h(OfficePageNavigator, { ...state, onNavigate: navigate, onClose: close, onExpand: expand }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  let alive = true
  const unmount = () => { if (alive) { alive = false; app.unmount(); host.remove() } }
  cleanups.push(unmount)
  await settle()
  return { host, state, navigate, close, expand, unmount }
}
function setRailBounds(host: HTMLElement) {
  const rail = host.querySelector<HTMLElement>('[role="slider"]')!
  vi.spyOn(rail, 'getBoundingClientRect').mockReturnValue({ top: 100, left: 0, width: 28, height: 216, right: 28, bottom: 316, x: 0, y: 100, toJSON() {} })
  return rail
}

beforeEach(() => {
  nextUrl = 0
  created.mockReset().mockImplementation(() => `blob:thumbnail-${++nextUrl}`)
  revoked.mockReset()
  vi.spyOn(URL, 'createObjectURL').mockImplementation(created)
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(revoked)
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resized = callback }
    observe() {}
    disconnect() {}
  })
})
afterEach(() => {
  cleanups.splice(0).forEach(cleanup => cleanup())
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks()
})

describe('Office page navigator', () => {
  it('virtualizes a thousand-page document and releases images outside the new window', async () => {
    const render = vi.fn(async () => thumbnail())
    const view = await mount({ session: session(render) })
    const firstUrls = Array.from(view.host.querySelectorAll('img')).map(image => image.src)
    expect(firstUrls.length).toBeGreaterThan(0)
    expect(view.host.querySelectorAll('[data-page]').length).toBeLessThanOrEqual(32)
    expect(render.mock.calls.length).toBeLessThanOrEqual(32)
    const list = view.host.querySelector<HTMLElement>('.office-page-nav__list')!
    list.scrollTop = 184 * 500
    list.dispatchEvent(new Event('scroll')); await settle()
    expect(view.host.querySelector('[data-page="0"]')).toBeNull()
    expect(view.host.querySelector('[data-page="500"]')).not.toBeNull()
    for (const url of firstUrls) expect(revoked).toHaveBeenCalledWith(url)
    // An unusually tall viewport must still have a fixed resource upper bound.
    resized([{ target: list, contentRect: { height: 12000 } } as unknown as ResizeObserverEntry], {} as ResizeObserver)
    await settle()
    expect(view.host.querySelectorAll('[data-page]').length).toBeLessThanOrEqual(32)
    expect(view.host.querySelectorAll('img').length).toBeLessThanOrEqual(32)
    view.unmount()
    expect(revoked).toHaveBeenCalledTimes(created.mock.calls.length)
  })

  it('navigates by thumbnail or keyboard, follows the active page, and leaves Escape to its parent', async () => {
    const view = await mount({ total: 100, active: 2, format: 'pptx' })
    const selected = view.host.querySelector<HTMLButtonElement>('[data-page="2"]')!
    expect(selected.getAttribute('aria-current')).toBe('page')
    selected.click()
    expect(view.navigate).toHaveBeenLastCalledWith(2)
    selected.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true }))
    await settle()
    expect(view.navigate).toHaveBeenLastCalledWith(99)
    expect((document.activeElement as HTMLElement).dataset.page).toBe('99')
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    document.activeElement!.dispatchEvent(escape)
    expect(escape.defaultPrevented).toBe(false)
    view.state.active = 50; await settle()
    expect(view.host.querySelector('[data-page="50"]')?.getAttribute('aria-current')).toBe('page')
    view.host.querySelector<HTMLButtonElement>('.office-page-nav__toggle')!.click()
    expect(view.close).toHaveBeenCalledOnce()
  })

  it('aborts an offscreen pending request and ignores stale bytes before processing the new window', async () => {
    const pending = deferred<OfficePageThumbnail>()
    const oldThumbnail = thumbnail('old')
    const render = vi.fn(async (_index: number, _signal?: AbortSignal) => thumbnail('new')).mockImplementationOnce(() => pending.promise)
    const view = await mount({ session: session(render) })
    expect(render).toHaveBeenCalledTimes(1)
    const signal = render.mock.calls[0]![1]!
    const list = view.host.querySelector<HTMLElement>('.office-page-nav__list')!
    list.scrollTop = 184 * 700; list.dispatchEvent(new Event('scroll')); await settle()
    expect(signal.aborted).toBe(true)
    expect(render).toHaveBeenCalledTimes(1)
    pending.resolve(oldThumbnail); await settle()
    expect(created).not.toHaveBeenCalledWith(oldThumbnail.blob)
    expect(view.host.querySelector('[data-page="700"] img')).not.toBeNull()
  })

  it('drops pending results from an old document and revokes new document URLs on unmount', async () => {
    const pending = deferred<OfficePageThumbnail>(), oldThumbnail = thumbnail('old document')
    const oldRender = vi.fn((_index: number, _signal?: AbortSignal) => pending.promise)
    const view = await mount({ session: session(oldRender), total: 2 })
    const nextRender = vi.fn(async () => thumbnail('replacement'))
    view.state.session = session(nextRender); await settle()
    expect(oldRender.mock.calls[0]![1]!.aborted).toBe(true)
    pending.resolve(oldThumbnail); await settle()
    expect(created).not.toHaveBeenCalledWith(oldThumbnail.blob)
    expect(nextRender).toHaveBeenCalledTimes(2)
    view.unmount()
    expect(revoked).toHaveBeenCalledTimes(2)
  })

  it('does not create URLs when a pending thumbnail finishes after unmount', async () => {
    const pending = deferred<OfficePageThumbnail>()
    const render = vi.fn((_index: number, _signal?: AbortSignal) => pending.promise)
    const view = await mount({ session: session(render) })
    view.unmount()
    expect(render.mock.calls[0]![1]!.aborted).toBe(true)
    pending.resolve(thumbnail()); await settle()
    expect(created).not.toHaveBeenCalled()
  })

  it('keeps a failed thumbnail navigable without repeatedly requesting the failed page', async () => {
    const render = vi.fn(async () => { throw new Error('bad image') })
    const view = await mount({ session: session(render), total: 1 })
    expect(view.host.querySelector('.office-page-nav__placeholder')?.getAttribute('aria-label')).toBe(en.workbench.office.thumbnailFailed)
    view.host.querySelector<HTMLButtonElement>('[data-page="0"]')!.click(); await settle()
    expect(view.navigate).toHaveBeenCalledWith(0)
    expect(render).toHaveBeenCalledTimes(1)
  })

  it('caps short-line markers, previews hover without navigating, and only renders the settled hover', async () => {
    vi.useFakeTimers()
    const render = vi.fn(async () => thumbnail())
    const view = await mount({ collapsed: true, total: 1000, session: session(render) })
    expect(view.host.querySelectorAll('.office-page-nav__line').length).toBeLessThanOrEqual(65)
    expect(render).not.toHaveBeenCalled()
    const rail = setRailBounds(view.host)
    rail.dispatchEvent(new PointerEvent('pointermove', { clientY: 208 }))
    await settle()
    expect(view.navigate).not.toHaveBeenCalled()
    const widths = Array.from(view.host.querySelectorAll<HTMLElement>('.office-page-nav__line:not(.is-active)')).map(line => Number.parseFloat(line.style.width))
    expect(Math.max(...widths)).toBeGreaterThan(Math.min(...widths))
    rail.dispatchEvent(new PointerEvent('pointermove', { clientY: 308 }))
    await vi.advanceTimersByTimeAsync(120); await settle()
    expect(render).toHaveBeenCalledExactlyOnceWith(999, expect.any(AbortSignal))
    expect(view.navigate).not.toHaveBeenCalled()
    rail.dispatchEvent(new MouseEvent('click', { clientY: 308, detail: 1, bubbles: true }))
    expect(view.navigate).toHaveBeenLastCalledWith(999)
    rail.dispatchEvent(new PointerEvent('pointerleave')); await settle()
    expect(view.host.querySelector('[role="tooltip"]')).toBeNull()
    expect(revoked).toHaveBeenCalledWith('blob:thumbnail-1')
  })

  it('supports keyboard-only rail navigation and isolates the expand button', async () => {
    vi.useFakeTimers()
    const view = await mount({ collapsed: true, total: 1000, active: 10 })
    const rail = setRailBounds(view.host)
    rail.focus(); await settle()
    expect(view.navigate).not.toHaveBeenCalled()
    const down = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true })
    rail.dispatchEvent(down)
    expect(down.defaultPrevented).toBe(true)
    expect(view.navigate).toHaveBeenLastCalledWith(11)
    rail.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageDown', bubbles: true }))
    expect(view.navigate).toHaveBeenLastCalledWith(21)
    rail.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(view.navigate).toHaveBeenLastCalledWith(999)
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    rail.dispatchEvent(escape)
    expect(escape.defaultPrevented).toBe(false)
    const previousCalls = view.navigate.mock.calls.length
    view.host.querySelector<HTMLButtonElement>('.office-page-nav__toggle')!.click()
    expect(view.expand).toHaveBeenCalledOnce()
    expect(view.navigate).toHaveBeenCalledTimes(previousCalls)
  })

  it('releases expanded thumbnails immediately when switching to the compact rail', async () => {
    const view = await mount({ total: 3 })
    expect(created).toHaveBeenCalledTimes(3)
    view.state.collapsed = true; await settle()
    expect(revoked).toHaveBeenCalledTimes(3)
    expect(view.host.querySelectorAll('img')).toHaveLength(0)
    expect(view.host.querySelector('[role="slider"]')).not.toBeNull()
  })
})
