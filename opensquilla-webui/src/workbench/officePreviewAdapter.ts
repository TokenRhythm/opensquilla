import type { OfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import { ensureOfficePreviewFonts } from '@/utils/workbench/officePreviewFonts'
import { projectOfficeSelection, type OfficeSelection } from '@/types/officeSelection'
import { normalizeBrowserUrl } from './browserItems'
import { createOfficeSelectionInteraction, type OfficeHitTarget, type OfficeSelectionLabels } from './officeSelectionInteraction'
import { createOfficeThumbnails, type OfficePageSize, type OfficePageThumbnail } from './officeThumbnails'

export type { OfficeSelectionLabels } from './officeSelectionInteraction'
export type { OfficePageSize, OfficePageThumbnail } from './officeThumbnails'
export interface OfficePreviewViewState {
  scale: number
  mode: 'page' | 'width' | 'manual'
  width: number
  height: number
}

export interface OfficePreviewSession {
  setSelectionMode(enabled: boolean): void
  clearSelection(): void
  destroy(): void
  previous(): Promise<void>
  next(): Promise<void>
  goTo(index: number): Promise<void>
  getPageSize(index: number): OfficePageSize | null
  getNotes(index: number): string | null
  renderThumbnail(index: number, signal?: AbortSignal): Promise<OfficePageThumbnail>
  fitPage(): void | Promise<void>
  fitWidth(): void | Promise<void>
  zoomIn(): void | Promise<void>
  zoomOut(): void | Promise<void>
  setScale?(scale: number): void | Promise<void>
  resize(): void | Promise<void>
}
export interface OfficePreviewOptions {
  container: HTMLElement
  /** Scroll viewport; separate from the intrinsic, centered canvas stage. */
  viewportElement?: HTMLElement
  viewportPadding?: number
  viewportBottomInset?: number
  data: ArrayBuffer
  format: OfficePreviewFormat
  signal: AbortSignal
  onSelection(selection: OfficeSelection | null): void
  onSelectionCommit?(selection: OfficeSelection): void
  onLocation(index: number, total: number): void
  onError(error: Error): void
  selectionLabels?: OfficeSelectionLabels
  onView?(state: OfficePreviewViewState): void
}

/** Same external-link policy as the Workbench; document links cannot navigate the Control UI. */
function openHyperlink(target: { kind: string; url?: string }) {
  if (target.kind !== 'external' || !target.url) return
  const url = normalizeBrowserUrl(target.url)
  if (!url || url === 'about:blank') return
  const opened = window.open(url, '_blank', 'noopener,noreferrer')
  if (opened) opened.opener = null
}

export async function createOfficePreviewSession(options: OfficePreviewOptions): Promise<OfficePreviewSession> {
  const { container, signal, format } = options
  // Custom document fonts are not inherited by OffscreenCanvas workers in 0.88.
  // Main drawing uses the same loaded FontFaceSet as the selectable text layer.
  await ensureOfficePreviewFonts()
  signal.throwIfAborted()
  const [{ math }, { tiff }] = await Promise.all([
    import('@silurus/ooxml/math'), import('@silurus/ooxml/tiff'),
  ])
  signal.throwIfAborted()
  let interaction: ReturnType<typeof createOfficeSelectionInteraction> | null = null
  let thumbnails: ReturnType<typeof createOfficeThumbnails> | null = null
  let destroyed = false
  let locationIndex = -1, locationTotal = 0
  let viewMode: OfficePreviewViewState['mode'] = format === 'pptx' ? 'page' : format === 'docx' ? 'width' : 'manual'
  const onLocation = (index: number, total: number) => {
    if (destroyed || signal.aborted) return
    if (index === locationIndex && total === locationTotal) return
    if (index !== locationIndex) interaction?.clearSelection()
    locationIndex = index; locationTotal = total
    options.onLocation(index, total)
  }
  const common = {
    mode: 'main' as const,
    useGoogleFonts: false,
    cjkFallback: 'sc' as const,
    maxZipEntryBytes: 64 * 1024 * 1024,
    resourceLimits: { maxArchiveEntries: 10_000, maxArchiveEntryBytes: 64 * 1024 * 1024,
      maxTotalInflatedBytes: 256 * 1024 * 1024 },
    math,
    tiff,
    imageResources: { decodedByteBudget: 64 * 1024 * 1024, strategy: 'adaptive' as const },
    // The vendor has no public element-selection reset/mode API. Keep its
    // element picker disabled and own the annotation feedback instead.
    enableElementSelection: false,
    enableHyperlinks: true,
    onHyperlinkClick: (target: Parameters<typeof openHyperlink>[0]) => {
      if (!interaction?.enabled) openHyperlink(target)
    },
    onSelectionContextChange: (context: unknown) => {
      if (!signal.aborted) interaction?.nativeSelection(projectOfficeSelection(context))
    },
    onError: (error: Error) => { if (!signal.aborted && !destroyed) options.onError(error) },
  }
  let viewer: { destroy(): void; fitWidth(): void | Promise<void>;
    fitPage(): void | Promise<void>; getScale(): number; setScale(scale: number): void | Promise<void>;
    canvasElement: HTMLCanvasElement; zoomIn(): void | Promise<void>; zoomOut(): void | Promise<void>; getSelectionContext(): unknown } | null = null
  let releaseDocument: (() => void) | null = null
  let previous: () => Promise<void>, next: () => Promise<void>, goTo: (index: number) => Promise<void>, resize: () => void | Promise<void>
  let getPageSize: (index: number) => OfficePageSize | null = () => null
  let getNotes: (index: number) => string | null = () => null
  let renderPage: ((canvas: HTMLCanvasElement, index: number, width: number) => Promise<void>) | null = null
  let hitTest: (x: number, y: number) => Promise<OfficeHitTarget | null>
  let clearNativeSelection = () => {}
  const destroy = () => {
    if (destroyed) return
    destroyed = true
    signal.removeEventListener('abort', destroy)
    interaction?.destroy()
    viewer?.destroy()
    const thumbnailCompletion = thumbnails?.destroy()
    // A public renderer cannot be aborted halfway through. Drop its result and
    // release the borrowed model after that one bounded render has settled.
    if (thumbnailCompletion) void thumbnailCompletion.then(() => releaseDocument?.())
    else releaseDocument?.()
    container.replaceChildren()
  }
  signal.addEventListener('abort', destroy, { once: true })
  try {
    signal.throwIfAborted()
    if (format === 'docx') {
      const { DocxDocument, DocxViewer } = await import('@silurus/ooxml/docx')
      signal.throwIfAborted()
      const document = await DocxDocument.load(options.data.slice(0), common)
      if (destroyed || signal.aborted) { document.destroy(); signal.throwIfAborted(); throw new Error('Preview disposed') }
      releaseDocument = () => document.destroy()
      const canvas = container.ownerDocument.createElement('canvas')
      container.append(canvas)
      const instance = DocxViewer.fromDocument(canvas, document, { ...common, container, enableTextSelection: true, onPageChange: onLocation })
      viewer = instance
      previous = () => instance.prevPage()
      next = () => instance.nextPage()
      goTo = index => instance.goToPage(index)
      resize = () => applyFit()
      getPageSize = index => {
        if (!Number.isInteger(index) || index < 0 || index >= document.pageCount) return null
        const size = document.pageSize(index)
        return { width: size.widthPt, height: size.heightPt }
      }
      renderPage = (canvas, index, width) => document.renderPage(canvas, index, { width, dpr: 1, imageResources: common.imageResources })
      let runsKey = '', runs: ReturnType<typeof document.collectPageRuns> | null = null
      hitTest = async (x, y) => {
        const rect = canvas.getBoundingClientRect(), page = instance.currentPage
        if (!inside(rect, x, y)) return null
        const size = document.pageSize(page)
        const context = await document.getElementContextAt(page, {
          xPt: (x - rect.left) / rect.width * size.widthPt,
          yPt: (y - rect.top) / rect.height * size.heightPt,
        })
        if (context) return { kind: 'element', selection: projectOfficeSelection(context) || undefined,
          rect: { left: rect.left + context.bounds.xPt / size.widthPt * rect.width,
            top: rect.top + context.bounds.yPt / size.heightPt * rect.height,
            width: context.bounds.widthPt / size.widthPt * rect.width, height: context.bounds.heightPt / size.heightPt * rect.height } }
        const key = `${page}:${rect.width}`
        if (key !== runsKey || !runs) { runsKey = key; runs = document.collectPageRuns(page, { width: rect.width }) }
        const run = (await runs).find(run => x >= rect.left + run.x && x <= rect.left + run.x + run.w
          && y >= rect.top + run.y && y <= rect.top + run.y + run.h)
        return run ? { kind: 'text', rect: { left: rect.left + run.x, top: rect.top + run.y, width: run.w, height: run.h } } : null
      }
      await instance.goToPage(0)
    } else if (format === 'pptx') {
      const { PptxPresentation, PptxViewer } = await import('@silurus/ooxml/pptx')
      signal.throwIfAborted()
      const presentation = await PptxPresentation.load(options.data.slice(0), common)
      if (destroyed || signal.aborted) { presentation.destroy(); signal.throwIfAborted(); throw new Error('Preview disposed') }
      releaseDocument = () => presentation.destroy()
      const canvas = container.ownerDocument.createElement('canvas')
      container.append(canvas)
      const instance = PptxViewer.fromPresentation(canvas, presentation, { ...common, enableTextSelection: true,
        enableMediaPlayback: false, onSlideChange: onLocation })
      viewer = instance
      previous = () => instance.prevSlide()
      next = () => instance.nextSlide()
      goTo = index => instance.goToSlide(index)
      resize = () => applyFit()
      getPageSize = index => Number.isInteger(index) && index >= 0 && index < presentation.slideCount
        ? { width: presentation.slideWidth, height: presentation.slideHeight } : null
      getNotes = index => getPageSize(index) ? presentation.getNotes(index) : null
      renderPage = (canvas, index, width) => presentation.renderSlide(canvas, index, { width, dpr: 1,
        skipMediaControls: true, imageResources: common.imageResources })
      hitTest = async (x, y) => {
        const rect = canvas.getBoundingClientRect()
        if (!inside(rect, x, y)) return null
        const context = await presentation.getElementContextAt(instance.slideIndex, {
          x: (x - rect.left) / rect.width * presentation.slideWidth,
          y: (y - rect.top) / rect.height * presentation.slideHeight,
        })
        if (!context) return null
        const bounds = context.bounds
        // Axis-aligned feedback encloses rotated objects; source identifiers
        // remain the public semantic context, not these screen coordinates.
        const angle = bounds.rotation * Math.PI / 180
        const width = Math.abs(bounds.width * Math.cos(angle)) + Math.abs(bounds.height * Math.sin(angle))
        const height = Math.abs(bounds.width * Math.sin(angle)) + Math.abs(bounds.height * Math.cos(angle))
        return { kind: 'element', selection: projectOfficeSelection(context) || undefined,
          rect: { left: rect.left + (bounds.x + (bounds.width - width) / 2) / presentation.slideWidth * rect.width,
            top: rect.top + (bounds.y + (bounds.height - height) / 2) / presentation.slideHeight * rect.height,
            width: width / presentation.slideWidth * rect.width, height: height / presentation.slideHeight * rect.height } }
      }
      await instance.goToSlide(0)
    } else {
      const { XlsxViewer } = await import('@silurus/ooxml/xlsx')
      signal.throwIfAborted()
      const instance = new XlsxViewer(container, { ...common, onSheetChange: onLocation,
        onViewportChange: () => { if (interaction?.enabled) interaction.invalidateFeedback() },
        onReady: names => onLocation(0, names.length), showZoomSlider: false })
      viewer = instance
      previous = () => instance.prevSheet()
      next = () => instance.nextSheet()
      goTo = index => instance.goToSheet(index)
      resize = () => instance.relayout()
      clearNativeSelection = () => instance.setSelection(null)
      hitTest = async (x, y) => {
        const cell = instance.getCellAt(x, y), rect = instance.canvasElement.getBoundingClientRect()
        if (!cell || !inside(rect, x, y)) return null
        const bounds = instance.getCellViewportRect(cell)
        return bounds ? { kind: 'range', rect: { left: rect.left + bounds.x, top: rect.top + bounds.y,
          width: bounds.width, height: bounds.height } } : null
      }
      await instance.load(options.data.slice(0))
    }
    signal.throwIfAborted()
    if (format !== 'xlsx') await applyFit()
    signal.throwIfAborted()
    if (renderPage) thumbnails = createOfficeThumbnails({ document: container.ownerDocument, getPageSize, render: renderPage })
    interaction = createOfficeSelectionInteraction({ container, labels: options.selectionLabels,
      hitTest, clearNativeSelection, readNativeSelection: () => projectOfficeSelection(viewer?.getSelectionContext()),
      onSelection: options.onSelection, onSelectionCommit: options.onSelectionCommit })
    const navigate = (action: () => Promise<void>) => async () => {
      if (destroyed) return
      interaction?.clearSelection()
      await action()
      if (!destroyed) await applyFit()
    }
    const fit = async (mode: 'page' | 'width') => {
      if (destroyed) return
      interaction?.clearSelection(); viewMode = mode
      await applyFit()
    }
    const zoom = async (action: () => void | Promise<void>) => {
      if (destroyed) return
      interaction?.clearSelection(); viewMode = 'manual'
      await action(); reportView()
    }
    return { destroy, setSelectionMode: value => interaction?.setSelectionMode(value),
      clearSelection: () => interaction?.clearSelection(), previous: navigate(previous), next: navigate(next),
      goTo: index => Number.isInteger(index) && index >= 0 && index < locationTotal ? navigate(() => goTo(index))() : Promise.resolve(),
      getPageSize: index => destroyed ? null : getPageSize(index), getNotes: index => destroyed ? null : getNotes(index),
      renderThumbnail: (index, requestSignal) => thumbnails?.request(index, requestSignal)
        ?? Promise.reject(new RangeError('Thumbnails are unavailable for this format')),
      resize: async () => {
        if (destroyed) return
        // Centering can move the page even when manual/height-fit scale stays
        // unchanged. Drop old screen geometry without discarding its selection.
        interaction?.invalidateFeedback()
        await resize(); reportView()
      },
      fitWidth: () => fit('width'), fitPage: () => fit('page'),
      setScale: scale => Number.isFinite(scale)
        ? zoom(() => viewer!.setScale(Math.max(0.1, Math.min(10, scale)))) : undefined,
      zoomIn: () => zoom(() => viewer!.zoomIn()), zoomOut: () => zoom(() => viewer!.zoomOut()) }
  } catch (error) {
    destroy()
    throw error
  }

  function reportView() {
    if (!viewer || destroyed) return
    const canvas = viewer.canvasElement
    options.onView?.({ scale: viewer.getScale(), mode: viewMode,
      width: Number.parseFloat(canvas.style.width) || canvas.getBoundingClientRect().width,
      height: Number.parseFloat(canvas.style.height) || canvas.getBoundingClientRect().height })
  }
  async function applyFit() {
    if (!viewer || destroyed) return
    if (viewMode === 'manual') { reportView(); return }
    if (format === 'xlsx') {
      await (viewMode === 'page' ? viewer.fitPage() : viewer.fitWidth())
      reportView(); return
    }
    const viewport = options.viewportElement || container
    const css = viewport.ownerDocument.defaultView!.getComputedStyle(viewport)
    const padding = options.viewportPadding
    const horizontal = padding !== undefined ? padding * 2 : (Number.parseFloat(css.paddingLeft) || 0) + (Number.parseFloat(css.paddingRight) || 0)
    const vertical = padding !== undefined ? padding * 2 : (Number.parseFloat(css.paddingTop) || 0) + (Number.parseFloat(css.paddingBottom) || 0)
    const width = viewport.clientWidth - horizontal
    const height = viewport.clientHeight - vertical - (options.viewportBottomInset || 0)
    const size = getPageSize(locationIndex)
    const currentWidth = Number.parseFloat(viewer.canvasElement.style.width) || viewer.canvasElement.getBoundingClientRect().width
    if (size && width > 0 && (viewMode === 'width' || height > 0) && currentWidth > 0) {
      const targetWidth = viewMode === 'page' ? Math.min(width, height * size.width / size.height) : width
      const scale = viewer.getScale() * targetWidth / currentWidth
      // A guide/notes height change must not rerender width-fit pages or reset
      // text selection when their effective drawing size did not change.
      if (Math.abs(scale - viewer.getScale()) > 0.0001) {
        interaction?.clearSelection()
        await viewer.setScale(scale)
      }
    } else if (!options.viewportElement) {
      await (viewMode === 'page' ? viewer.fitPage() : viewer.fitWidth())
    }
    reportView()
  }
}

function inside(rect: DOMRect, x: number, y: number): boolean {
  return rect.width > 0 && rect.height > 0 && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom
}
