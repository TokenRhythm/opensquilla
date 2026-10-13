// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createOfficePreviewSession, type OfficePreviewOptions } from './officePreviewAdapter'

type ViewerCallbacks = {
  enableElementSelection?: boolean
  onSelectionContextChange?: (value: unknown) => void
  onHyperlinkClick?: (value: { kind: string; url?: string }) => void
  onPageChange?: (index: number, total: number) => void
  onSlideChange?: (index: number, total: number) => void
  onSheetChange?: (index: number, total: number) => void
  onViewportChange?: () => void
}
const vendor = vi.hoisted(() => ({
  callbacks: {} as ViewerCallbacks,
  release: vi.fn(), destroy: vi.fn(), load: vi.fn(), fromModel: vi.fn(), clear: vi.fn(),
  getSelectionContext: vi.fn(),
  goTo: vi.fn(), setScale: vi.fn(), fitWidth: vi.fn(), fitPage: vi.fn(),
}))
vi.mock('@/utils/workbench/officePreviewFonts', () => ({ ensureOfficePreviewFonts: vi.fn() }))
vi.mock('@silurus/ooxml/math', () => ({ math: {} }))
vi.mock('@silurus/ooxml/tiff', () => ({ tiff: {} }))

function model() {
  return { destroy: vendor.release, pageCount: 2, slideCount: 2, pageSize: () => ({ widthPt: 600, heightPt: 800 }),
    slideWidth: 960, slideHeight: 540, getElementContextAt: vi.fn(async (): Promise<unknown> => null),
    getNotes: vi.fn((index: number) => index === 1 ? '中文演讲者备注\n<script>保持纯文本</script>' : null),
    renderPage: vi.fn(async (canvas: HTMLCanvasElement, _index: number, opts: { width: number }) => { canvas.width = opts.width; canvas.height = Math.round(opts.width * 4 / 3) }),
    renderSlide: vi.fn(async (canvas: HTMLCanvasElement, _index: number, opts: { width: number }) => { canvas.width = opts.width; canvas.height = Math.round(opts.width * 9 / 16) }),
    collectPageRuns: vi.fn(async () => [] as Array<{ x: number; y: number; w: number; h: number }>) }
}
function viewer(canvas: HTMLCanvasElement, callbacks: ViewerCallbacks) {
  vendor.callbacks = callbacks
  let index = 0, scale = 1
  const width = callbacks.onSlideChange ? 960 : 800, height = callbacks.onSlideChange ? 540 : 800 * 4 / 3
  canvas.style.width = `${width}px`; canvas.style.height = `${height}px`
  const navigate = async (value: number) => { vendor.goTo(value); index = value; (callbacks.onPageChange || callbacks.onSlideChange)?.(index, 2) }
  const setScale = async (value: number) => {
    vendor.setScale(value); scale = value
    canvas.style.width = `${width * scale}px`; canvas.style.height = `${height * scale}px`
    ;(callbacks.onPageChange || callbacks.onSlideChange)?.(index, 2)
  }
  return { canvasElement: canvas, get currentPage() { return index }, get slideIndex() { return index },
    destroy: vendor.destroy, fitWidth: vendor.fitWidth, fitPage: vendor.fitPage,
    getScale: () => scale, setScale, zoomIn: () => setScale(scale * 1.25), zoomOut: () => setScale(scale / 1.25),
    getSelectionContext: vendor.getSelectionContext,
    goToPage: navigate, goToSlide: navigate,
    prevPage: () => navigate(0), nextPage: () => navigate(1), prevSlide: () => navigate(0), nextSlide: () => navigate(1),
  }
}
vi.mock('@silurus/ooxml/docx', () => ({
  DocxDocument: { load: (...args: unknown[]) => vendor.load(...args) },
  DocxViewer: { fromDocument: (canvas: HTMLCanvasElement, _model: unknown, opts: ViewerCallbacks) => { vendor.fromModel(); return viewer(canvas, opts) } },
}))
vi.mock('@silurus/ooxml/pptx', () => ({
  PptxPresentation: { load: (...args: unknown[]) => vendor.load(...args) },
  PptxViewer: { fromPresentation: (canvas: HTMLCanvasElement, _model: unknown, opts: ViewerCallbacks) => { vendor.fromModel(); return viewer(canvas, opts) } },
}))
vi.mock('@silurus/ooxml/xlsx', () => ({ XlsxViewer: class {
  canvasElement = document.createElement('canvas')
  constructor(container: HTMLElement, opts: ViewerCallbacks) { container.append(this.canvasElement); vendor.callbacks = opts }
  load = vendor.load
  destroy = vendor.destroy
  fitWidth = vi.fn(); fitPage = vi.fn(); zoomIn = vi.fn(); zoomOut = vi.fn(); relayout = vi.fn()
  getScale = () => 1; setScale = vi.fn()
  getCellAt = vi.fn(() => ({ row: 1, col: 1 }))
  getCellViewportRect = vi.fn(() => ({ x: 50, y: 22, width: 80, height: 24 }))
  setSelection = vendor.clear
  getSelectionContext = vendor.getSelectionContext
  async prevSheet() { vendor.callbacks.onSheetChange?.(0, 2) }
  async nextSheet() { vendor.callbacks.onSheetChange?.(1, 2) }
  async goToSheet(index: number) { vendor.goTo(index); vendor.callbacks.onSheetChange?.(index, 2) }
} }))

let frames: Map<number, FrameRequestCallback>, frameId: number
beforeEach(() => {
  vi.clearAllMocks(); vendor.load.mockResolvedValue(model()); vendor.getSelectionContext.mockReturnValue(null)
  frames = new Map(); frameId = 0
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => { frames.set(++frameId, callback); return frameId })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => { frames.delete(id) })
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['png'], { type: 'image/png' })))
})
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks() })
async function flushFrame() {
  const callbacks = [...frames.values()]; frames.clear()
  callbacks.forEach(callback => callback(0))
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
}
async function harness(format: 'docx' | 'pptx' | 'xlsx', overrides: Partial<OfficePreviewOptions> = {}) {
  const container = document.createElement('div'); document.body.append(container)
  const controller = new AbortController(), onSelection = vi.fn(), onSelectionCommit = vi.fn(), onLocation = vi.fn()
  const session = await createOfficePreviewSession({ container, format, signal: controller.signal,
    data: new ArrayBuffer(8), onSelection, onSelectionCommit, onLocation, onError: vi.fn(), ...overrides })
  return { container, controller, onSelection, onSelectionCommit, onLocation, session }
}

describe('Office adapter public selection lifecycle', () => {
  it.each(['docx', 'pptx'] as const)('parses %s once and clears selections without recreating a viewer', async format => {
    const h = await harness(format)
    const context = { format, kind: 'text', text: '中文选区', truncated: false }
    expect(vendor.callbacks.enableElementSelection).toBe(false)
    vendor.callbacks.onSelectionContextChange?.(context)
    expect(h.onSelection).not.toHaveBeenCalled()
    h.session.setSelectionMode(true)
    vendor.callbacks.onSelectionContextChange?.(context)
    expect(h.onSelection).toHaveBeenLastCalledWith(context)
    h.session.setSelectionMode(false)
    vendor.callbacks.onSelectionContextChange?.(context)
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    h.session.setSelectionMode(true)
    await h.session.next()
    expect(h.onLocation).toHaveBeenLastCalledWith(1, 2)
    vendor.callbacks.onSelectionContextChange?.(context)
    expect(h.onSelection).toHaveBeenLastCalledWith(context)
    expect(vendor.load).toHaveBeenCalledTimes(1)
    expect(vendor.fromModel).toHaveBeenCalledTimes(1)
    h.session.destroy(); h.session.destroy()
    expect(vendor.destroy).toHaveBeenCalledTimes(1)
    expect(vendor.release).toHaveBeenCalledTimes(1)
  })

  it('prevents document hyperlinks from navigating while annotation mode is active', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const h = await harness('pptx')
    vendor.callbacks.onHyperlinkClick?.({ kind: 'external', url: 'https://example.com' })
    expect(open).toHaveBeenCalledTimes(1)
    h.session.setSelectionMode(true)
    vendor.callbacks.onHyperlinkClick?.({ kind: 'external', url: 'https://example.com' })
    expect(open).toHaveBeenCalledTimes(1)
    h.session.destroy()
  })

  it('leaves normal sheet selection outside annotation context and preserves a range during viewport scrolling', async () => {
    const h = await harness('xlsx')
    const range = { format: 'xlsx', kind: 'range', sheetIndex: 0, sheetName: 'Sheet 1',
      selection: { areas: [{ kind: 'cells', top: 1, bottom: 2, left: 1, right: 2 }] }, cells: [], truncated: false }
    vendor.callbacks.onSelectionContextChange?.(range)
    expect(h.onSelection).not.toHaveBeenCalled()
    h.session.setSelectionMode(true)
    vendor.callbacks.onSelectionContextChange?.(range)
    expect(h.onSelection).toHaveBeenLastCalledWith(expect.objectContaining({ range: 'A1:B2' }))
    const clears = vendor.clear.mock.calls.length
    vendor.callbacks.onViewportChange?.()
    expect(vendor.clear).toHaveBeenCalledTimes(clears)
    vendor.callbacks.onSheetChange?.(1, 2)
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    expect(vendor.clear).toHaveBeenLastCalledWith(null)
    h.session.setSelectionMode(false)
    expect(vendor.load).toHaveBeenCalledTimes(1)
    h.session.destroy()
  })

  it('releases a parsed model if cancellation arrives before it can be mounted', async () => {
    let resolve!: (value: ReturnType<typeof model>) => void
    vendor.load.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const container = document.createElement('div'), controller = new AbortController()
    const pending = createOfficePreviewSession({ container, format: 'docx', signal: controller.signal,
      data: new ArrayBuffer(8), onSelection: vi.fn(), onLocation: vi.fn(), onError: vi.fn() })
    await vi.waitFor(() => expect(vendor.load).toHaveBeenCalled())
    controller.abort(); resolve(model())
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(vendor.release).toHaveBeenCalledTimes(1)
    expect(vendor.fromModel).not.toHaveBeenCalled()
  })

  it.each(['docx', 'pptx', 'xlsx'] as const)('reads the final %s public selection only after a completed gesture', async format => {
    const source = model()
    source.collectPageRuns.mockResolvedValue([{ x: 0, y: 0, w: 100, h: 100 }])
    if (format === 'pptx') source.getElementContextAt.mockResolvedValue({ format, kind: 'element', slideIndex: 0,
      shapeId: '4', text: '标题', truncated: false, bounds: { x: 0, y: 0, width: 100, height: 100, rotation: 0 } })
    vendor.load.mockResolvedValue(source)
    const h = await harness(format), canvas = h.container.querySelector('canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 400, 300))
    const text = { format, kind: 'text', text: '最终文字', truncated: false }
    const range = { format: 'xlsx', kind: 'range', sheetIndex: 0, sheetName: 'Sheet 1',
      selection: { areas: [{ kind: 'cells', top: 1, bottom: 2, left: 1, right: 2 }] }, cells: [], truncated: false }
    const context = format === 'xlsx' ? range : text
    h.session.setSelectionMode(true)
    canvas.dispatchEvent(new PointerEvent('pointerdown', { clientX: 50, clientY: 50, button: 0, pointerId: 7, bubbles: true }))
    vendor.callbacks.onSelectionContextChange?.(context)
    vendor.callbacks.onSelectionContextChange?.(context)
    await flushFrame()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    expect(vendor.getSelectionContext).not.toHaveBeenCalled()
    window.dispatchEvent(new PointerEvent('pointerup', { button: 0, pointerId: 7 }))
    await flushFrame()
    vendor.getSelectionContext.mockReturnValue(context)
    await flushFrame()
    expect(vendor.getSelectionContext).toHaveBeenCalledTimes(1)
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(format === 'xlsx'
      ? expect.objectContaining({ format, kind: 'range', range: 'A1:B2' }) : text)
    vendor.callbacks.onSelectionContextChange?.(context); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(1)
    h.session.destroy()
  })
})

describe('Office paged reading API', () => {
  it.each(['docx', 'pptx'] as const)('shares the parsed %s model for navigation and thumbnails', async format => {
    const source = model(); vendor.load.mockResolvedValue(source)
    const h = await harness(format)
    await h.session.goTo(1)
    expect(h.onLocation).toHaveBeenLastCalledWith(1, 2)
    expect(h.session.getPageSize(1)).toEqual(format === 'docx' ? { width: 600, height: 800 } : { width: 960, height: 540 })
    const thumbnail = await h.session.renderThumbnail(1)
    expect(thumbnail).toMatchObject({ blob: expect.any(Blob), width: 160 })
    expect(format === 'docx' ? source.renderPage : source.renderSlide).toHaveBeenCalledWith(expect.any(HTMLCanvasElement), 1,
      expect.objectContaining({ width: 160, dpr: 1 }))
    expect(await h.session.renderThumbnail(1)).toBe(thumbnail)
    expect(vendor.load).toHaveBeenCalledTimes(1)
    expect(vendor.fromModel).toHaveBeenCalledTimes(1)
    const navigations = vendor.goTo.mock.calls.length
    await h.session.goTo(-1); await h.session.goTo(2); await h.session.goTo(Number.NaN)
    expect(vendor.goTo).toHaveBeenCalledTimes(navigations)
    h.session.destroy()
    expect(h.session.getPageSize(1)).toBeNull()
  })

  it('returns speaker notes as plain text and keeps them separate from the selection pipeline', async () => {
    const h = await harness('pptx')
    expect(h.session.getNotes(0)).toBeNull()
    expect(h.session.getNotes(1)).toBe('中文演讲者备注\n<script>保持纯文本</script>')
    expect(h.session.getNotes(2)).toBeNull()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    h.session.destroy()
    expect(h.session.getNotes(1)).toBeNull()
  })

  it('fits the real viewport, keeps manual zoom on resize, and leaves unchanged width-fit selection alone', async () => {
    const viewport = document.createElement('div'); document.body.append(viewport)
    let width = 1000, height = 700
    Object.defineProperties(viewport, { clientWidth: { get: () => width }, clientHeight: { get: () => height } })
    const onView = vi.fn()
    const h = await harness('pptx', { viewportElement: viewport, viewportPadding: 24, viewportBottomInset: 112, onView })
    expect(vendor.setScale).toHaveBeenLastCalledWith(Math.min(952 / 960, 540 / 540))
    expect(onView).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'page', width: 952 }))
    await h.session.zoomIn()
    const manualScale = vendor.setScale.mock.calls.slice(-1)[0]![0]
    vendor.setScale.mockClear(); width = 800; height = 600
    await h.session.resize()
    expect(vendor.setScale).not.toHaveBeenCalled()
    expect(onView).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'manual', scale: manualScale }))
    await h.session.fitWidth()
    expect(vendor.setScale.mock.calls.slice(-1)[0]![0]).toBeCloseTo(752 / 960)
    h.session.setSelectionMode(true)
    const selected = { format: 'pptx', kind: 'text', text: '正在选择', truncated: false }
    vendor.callbacks.onSelectionContextChange?.(selected)
    h.onSelection.mockClear(); vendor.setScale.mockClear(); const locations = h.onLocation.mock.calls.length
    height = 400; await h.session.resize()
    expect(vendor.setScale).not.toHaveBeenCalled()
    expect(h.onSelection).not.toHaveBeenCalled()
    width = 900; await h.session.resize()
    expect(vendor.setScale.mock.calls.slice(-1)[0]![0]).toBeCloseTo(852 / 960)
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    expect(h.onLocation).toHaveBeenCalledTimes(locations)
    h.session.destroy()
  })

  it('defaults DOCX to width fit and measures CSS padding when no explicit padding is passed', async () => {
    const viewport = document.createElement('div'); viewport.style.padding = '20px'; document.body.append(viewport)
    Object.defineProperties(viewport, { clientWidth: { value: 840 }, clientHeight: { value: 500 } })
    const onView = vi.fn(), h = await harness('docx', { viewportElement: viewport, onView })
    expect(onView).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'width', width: 800, scale: 1 }))
    expect(vendor.setScale).not.toHaveBeenCalled()
    await h.session.fitPage()
    expect(vendor.setScale).toHaveBeenLastCalledWith((460 * 600 / 800) / 800)
    h.session.destroy()
  })

  it.each(['manual', 'page'] as const)('invalidates moved %s feedback while preserving semantic and native selection', async mode => {
    const source = model()
    source.getElementContextAt.mockResolvedValue({ format: 'pptx', kind: 'element', slideIndex: 0,
      shapeId: '4', text: '标题', truncated: false, bounds: { x: 0, y: 0, width: 100, height: 100, rotation: 0 } })
    vendor.load.mockResolvedValue(source)
    const viewport = document.createElement('div'); document.body.append(viewport)
    let width = 1000
    Object.defineProperties(viewport, { clientWidth: { get: () => width }, clientHeight: { value: 400 } })
    const h = await harness('pptx', { viewportElement: viewport, viewportPadding: 24, viewportBottomInset: 112 })
    if (mode === 'manual') await h.session.zoomIn()
    const canvas = h.container.querySelector('canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue(new DOMRect(100, 0, 400, 225))
    vi.spyOn(h.container, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 800, 400))
    h.session.setSelectionMode(true)
    canvas.dispatchEvent(new PointerEvent('pointerdown', { clientX: 120, clientY: 20, button: 0, pointerId: 3, bubbles: true }))
    canvas.dispatchEvent(new MouseEvent('click', { clientX: 120, clientY: 20, button: 0, bubbles: true }))
    const selected = h.container.querySelector<HTMLElement>('[data-office-selected]')!
    await vi.waitFor(() => expect(selected.style.display).toBe('block'))
    expect(h.onSelection).toHaveBeenLastCalledWith(expect.objectContaining({ kind: 'element', shapeId: '4' }))
    canvas.dispatchEvent(new PointerEvent('pointermove', { clientX: 120, clientY: 20, bubbles: true }))
    await flushFrame()
    const hover = h.container.querySelector<HTMLElement>('[data-office-hover]')!
    expect(hover.style.display).toBe('block')
    const text = document.createTextNode('仍可复制的文字'); h.container.append(text)
    const range = document.createRange(); range.selectNodeContents(text)
    document.getSelection()!.addRange(range)
    h.onSelection.mockClear(); vendor.setScale.mockClear()
    width = 850 // The sidebar moves a centered, height-constrained page without scaling it.
    await h.session.resize()
    expect(selected.style.display).toBe('none')
    expect(hover.style.display).toBe('none')
    expect(vendor.setScale).not.toHaveBeenCalled()
    expect(h.onSelection).not.toHaveBeenCalled()
    expect(document.getSelection()!.toString()).toBe('仍可复制的文字')
    h.session.destroy()
  })

  it('keeps the borrowed model alive until a cancelled thumbnail render settles', async () => {
    const source = model()
    let finish!: () => void
    source.renderSlide.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    vendor.load.mockResolvedValue(source)
    const h = await harness('pptx'), pending = h.session.renderThumbnail(0).catch(error => error)
    h.controller.abort()
    expect(await pending).toMatchObject({ name: 'AbortError' })
    expect(vendor.destroy).toHaveBeenCalledTimes(1)
    expect(vendor.release).not.toHaveBeenCalled()
    finish(); await vi.waitFor(() => expect(vendor.release).toHaveBeenCalledTimes(1))
    expect(source.renderSlide).toHaveBeenCalledTimes(1)
  })
})
