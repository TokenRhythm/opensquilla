// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFPageProxy, RenderTask } from 'pdfjs-dist'
import PdfResourcePreview from './PdfResourcePreview.vue'
import { createPdfLoadingTask, PDF_MAX_CANVAS_PIXELS, PDF_MAX_CANVAS_SIDE } from '@/workbench/pdfPreview'
import en from '@/locales/en.json'

vi.mock('@/workbench/pdfPreview', async importOriginal => ({
  ...await importOriginal<typeof import('@/workbench/pdfPreview')>(), createPdfLoadingTask: vi.fn(),
}))
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const cleanups: (() => void)[] = []
let pageCleanup = vi.fn(), renderPage = vi.fn(), getPage = vi.fn(), destroy = vi.fn()
let resizeCallback: ResizeObserverCallback
const disconnect = vi.fn()
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function pdfPage(width = 600, height = 800): PDFPageProxy {
  return { cleanup: pageCleanup, getViewport: ({ scale }: { scale: number }) => ({ width: width * scale, height: height * scale }), render: renderPage } as unknown as PDFPageProxy
}
function pdfDocument(count = 3): PDFDocumentProxy { return { numPages: count, getPage } as unknown as PDFDocumentProxy }
function loadingTask(promise = Promise.resolve(pdfDocument())): PDFDocumentLoadingTask {
  return { promise, destroy } as unknown as PDFDocumentLoadingTask
}
async function mount() {
  const host = document.createElement('div'); document.body.append(host)
  const state = reactive({ data: new ArrayBuffer(5), name: 'Example.pdf' })
  const app = createApp({ render: () => h(PdfResourcePreview, state) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })); app.mount(host)
  const unmount = () => { app.unmount(); host.remove() }
  cleanups.push(unmount); await flush()
  const button = (label: string) => host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  return { host, state, unmount, button }
}
beforeEach(() => {
  pageCleanup = vi.fn(); renderPage = vi.fn().mockImplementation(() => ({ promise: Promise.resolve(), cancel: vi.fn() }))
  getPage = vi.fn().mockImplementation(async () => pdfPage()); destroy = vi.fn().mockResolvedValue(undefined)
  vi.mocked(createPdfLoadingTask).mockReset().mockImplementation(async () => loadingTask())
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(632)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(832)
  vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(() => {})
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resizeCallback = callback }
    observe() {}
    disconnect = disconnect
  })
})
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('PDF resource preview', () => {
  it('renders only the current page and supports bounded direct navigation', async () => {
    const panel = await mount()
    expect(getPage.mock.calls).toEqual([[1]])
    expect(panel.host.querySelectorAll('canvas')).toHaveLength(1)
    expect(panel.host.textContent).toContain('/ 3')
    expect(panel.button(en.workbench.office.previous).disabled).toBe(true)
    panel.button(en.workbench.office.next).click(); await flush()
    expect(getPage.mock.calls).toEqual([[1], [2]])
    expect(pageCleanup).toHaveBeenCalled()
    const input = panel.host.querySelector<HTMLInputElement>('input')!
    input.value = '100'; input.dispatchEvent(new Event('change')); await flush()
    expect(getPage).toHaveBeenLastCalledWith(3)
    expect(input.value).toBe('3')
    expect(panel.button(en.workbench.office.next).disabled).toBe(true)
    input.value = '-10'; input.dispatchEvent(new Event('change')); await flush()
    expect(getPage).toHaveBeenLastCalledWith(1)
    expect(panel.host.querySelector('canvas')?.getAttribute('aria-label')).toContain('Example.pdf — Page 1')
  })

  it('shows a parse error, destroys its worker, and permits retry', async () => {
    vi.mocked(createPdfLoadingTask).mockImplementationOnce(async () => loadingTask(Promise.reject(new Error('Invalid PDF'))))
    const panel = await mount()
    expect(panel.host.querySelector('[role="alert"]')).not.toBeNull()
    expect(destroy).toHaveBeenCalledOnce()
    expect(getPage).not.toHaveBeenCalled()
    const retry = [...panel.host.querySelectorAll('button')].find(button => button.textContent === en.chat.retry)!
    retry.click(); await flush()
    expect(panel.host.querySelector('[role="alert"]')).toBeNull()
    expect(panel.host.querySelector('canvas')).not.toBeNull()
  })

  it('aborts document loading on replacement and ignores a late old result', async () => {
    const pending = deferred<PDFDocumentProxy>(), oldGetPage = vi.fn()
    vi.mocked(createPdfLoadingTask).mockImplementationOnce(async () => loadingTask(pending.promise))
    const panel = await mount()
    const signal = vi.mocked(createPdfLoadingTask).mock.calls[0]![1]
    panel.state.data = new ArrayBuffer(8); await flush()
    expect(signal.aborted).toBe(true)
    expect(destroy).toHaveBeenCalledOnce()
    pending.resolve({ numPages: 99, getPage: oldGetPage } as unknown as PDFDocumentProxy); await flush()
    expect(oldGetPage).not.toHaveBeenCalled()
    expect(panel.host.textContent).toContain('/ 3')
  })

  it('cleans up a late loading task returned after cancellation', async () => {
    const pending = deferred<PDFDocumentLoadingTask>(), oldDestroy = vi.fn().mockResolvedValue(undefined)
    vi.mocked(createPdfLoadingTask).mockImplementationOnce(() => pending.promise)
    const panel = await mount()
    panel.state.data = new ArrayBuffer(9); await flush()
    pending.resolve({ promise: Promise.resolve(pdfDocument(99)), destroy: oldDestroy } as unknown as PDFDocumentLoadingTask)
    await flush()
    expect(oldDestroy).toHaveBeenCalledOnce()
    expect(panel.host.textContent).toContain('/ 3')
  })

  it('cancels the previous render before replacing its canvas and ignores late errors', async () => {
    const pending = deferred<void>(), cancel = vi.fn(() => pending.reject(Object.assign(new Error('cancelled'), { name: 'RenderingCancelledException' })))
    renderPage.mockImplementationOnce(() => ({ promise: pending.promise, cancel } as unknown as RenderTask))
    const panel = await mount(), firstCanvas = panel.host.querySelector('canvas')
    panel.button(en.workbench.office.next).click(); await flush()
    expect(cancel).toHaveBeenCalledOnce()
    expect(panel.host.querySelector('canvas')).not.toBe(firstCanvas)
    expect(panel.host.querySelector('[role="alert"]')).toBeNull()
    expect(panel.host.querySelector('[aria-busy="true"]')).toBeNull()
  })

  it('releases a stale getPage response without rendering it', async () => {
    const pending = deferred<PDFPageProxy>(), staleCleanup = vi.fn(), staleRender = vi.fn()
    getPage.mockImplementationOnce(() => pending.promise)
    const panel = await mount()
    panel.button(en.workbench.office.next).click(); await flush()
    pending.resolve({ ...pdfPage(), cleanup: staleCleanup, render: staleRender } as unknown as PDFPageProxy); await flush()
    expect(staleCleanup).toHaveBeenCalled()
    expect(staleRender).not.toHaveBeenCalled()
    expect(panel.host.querySelector('[role="alert"]')).toBeNull()
  })

  it('surfaces renderer errors and clears a failed page', async () => {
    renderPage.mockImplementationOnce(() => ({ promise: Promise.reject(new Error('Decode failed')), cancel: vi.fn() }))
    const panel = await mount()
    expect(panel.host.querySelector('[role="alert"]')).not.toBeNull()
    expect(panel.host.querySelector('canvas')).toBeNull()
  })

  it('clamps zoom to 25–400 percent and can restore fit modes', async () => {
    const panel = await mount()
    for (let i = 0; i < 18; i++) { panel.button(en.workbench.office.zoomIn).click(); await flush() }
    expect(panel.button(en.workbench.office.zoomIn).disabled).toBe(true)
    expect(panel.host.querySelector('select')?.textContent).toContain('400%')
    for (let i = 0; i < 18; i++) { panel.button(en.workbench.office.zoomOut).click(); await flush() }
    expect(panel.button(en.workbench.office.zoomOut).disabled).toBe(true)
    expect(panel.host.querySelector('select')?.textContent).toContain('25%')
    const select = panel.host.querySelector('select')!
    select.value = 'page'; select.dispatchEvent(new Event('change')); await flush()
    expect(renderPage.mock.calls[renderPage.mock.calls.length - 1]![0].viewport).toMatchObject({ width: 600, height: 800 })
    expect(select.value).toBe('page')
    select.value = 'width'; select.dispatchEvent(new Event('change')); await flush()
    expect(select.value).toBe('width')
  })

  it('bounds huge page canvas allocation at high device density', async () => {
    getPage.mockResolvedValue(pdfPage(100_000, 100_000))
    vi.stubGlobal('devicePixelRatio', 3)
    const panel = await mount(), canvas = panel.host.querySelector('canvas')!
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(PDF_MAX_CANVAS_PIXELS)
    expect(Math.max(canvas.width, canvas.height)).toBeLessThanOrEqual(PDF_MAX_CANVAS_SIDE)
  })

  it('coalesces resize renders in fit mode and preserves manual zoom', async () => {
    let frame: FrameRequestCallback | null = null
    const request = vi.fn((callback: FrameRequestCallback) => { frame = callback; return 1 })
    vi.stubGlobal('requestAnimationFrame', request)
    const panel = await mount()
    const resized = (width: number) => resizeCallback([{ contentRect: { width, height: 500 } }] as ResizeObserverEntry[], {} as ResizeObserver)
    resized(500); resized(400)
    expect(request).toHaveBeenCalledOnce()
    ;(frame as unknown as FrameRequestCallback)(0); await flush()
    expect(getPage).toHaveBeenCalledTimes(2)
    panel.button(en.workbench.office.zoomIn).click(); await flush()
    resized(300)
    expect(request).toHaveBeenCalledOnce()
  })

  it('disconnects resize, aborts resources, cancels rendering and destroys the worker on unmount', async () => {
    const pending = deferred<void>(), cancel = vi.fn(() => pending.reject(new Error('cancelled')))
    renderPage.mockImplementationOnce(() => ({ promise: pending.promise, cancel }))
    const panel = await mount(), signal = vi.mocked(createPdfLoadingTask).mock.calls[0]![1]
    panel.unmount(); cleanups.pop(); await flush()
    expect(signal.aborted).toBe(true)
    expect(cancel).toHaveBeenCalledOnce()
    expect(destroy).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalled()
    expect(pageCleanup).toHaveBeenCalled()
  })
})
