// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OfficePreviewPanel from './OfficePreviewPanel.vue'
import { createOfficePreviewSession, type OfficePreviewOptions } from '@/workbench/officePreviewAdapter'
import type { OfficeSelection } from '@/types/officeSelection'
import type { OfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import en from '@/locales/en.json'
import type { ResourceReadingState } from './resourceReadingState'

vi.mock('@/workbench/officePreviewAdapter', () => ({ createOfficePreviewSession: vi.fn() }))
vi.mock('./OfficePageNavigator.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({
  props: { collapsed: Boolean, active: Number, total: Number }, emits: ['navigate', 'close', 'expand'],
  setup(props, { emit }) { return () => h('div', { class: 'test-page-navigator', 'data-collapsed': props.collapsed }, [
    h('button', { class: 'test-go-page', onClick: () => emit('navigate', 1) }, 'Page 2'),
    h('button', { class: 'test-expand', onClick: () => emit('expand') }, 'Expand'),
  ]) },
}) }
})

const sessions: OfficePreviewOptions[] = []
const cleanups: (() => void)[] = []
let observeResize: ResizeObserverCallback
const resize = vi.fn(), refreshAnchor = vi.fn()
const setSelectionMode = vi.fn()
const clearSelection = vi.fn()
const locateSelection = vi.fn(async () => ({ status: 'location-only' as const, anchor: null }))
const goTo = vi.fn()
const fitPage = vi.fn(), fitWidth = vi.fn(), setScale = vi.fn()
const title: OfficeSelection = { format: 'pptx', kind: 'element', slideIndex: 0, shapeId: '4', text: '中文标题', truncated: false }
async function flush() { await Promise.resolve(); await nextTick(); await nextTick() }

beforeEach(() => {
  sessions.length = 0
  resize.mockClear()
  setSelectionMode.mockClear()
  clearSelection.mockClear()
  goTo.mockReset(); fitPage.mockReset(); fitWidth.mockReset(); setScale.mockReset()
  vi.mocked(createOfficePreviewSession).mockImplementation(async options => {
    sessions.push(options)
    options.onLocation(0, 2)
    goTo.mockImplementation(async index => options.onLocation(index, 2))
    return { destroy: vi.fn(), previous: vi.fn(), next: vi.fn(), fitWidth, fitPage, goTo, setScale,
      getPageSize: () => ({ width: 960, height: 540 }), renderThumbnail: vi.fn(),
      getNotes: (index: number) => options.format === 'pptx' ? ['中文演讲者备注\n第二段 <script>不会执行</script>', '第二页备注'][index] || null : null,
      zoomIn: vi.fn(), zoomOut: vi.fn(), resize, refreshAnchor, setSelectionMode, clearSelection,
      clearTransientSelection: clearSelection, reselect: vi.fn(), locateSelection }
  })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { observeResize = callback }
    observe() {}
    disconnect() {}
  })
})
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.unstubAllGlobals(); vi.clearAllMocks() })

async function mount(format: OfficePreviewFormat = 'pptx', annotationEnabled = true, readingState?: ResourceReadingState, selectionMode?: boolean) {
  const host = document.createElement('div')
  document.body.append(host)
  const state = reactive({ data: new ArrayBuffer(1), format, annotationEnabled, suspended: false, readingState, selectionMode, selectionLocked: false,
    selectionToLocate: undefined as { selection: OfficeSelection; revisionId?: string; stale?: boolean; requestId: string } | undefined })
  const request = vi.fn(), commit = vi.fn(), anchor = vi.fn(), located = vi.fn()
  const app = createApp({ render: () => h(OfficePreviewPanel, { ...state, onSelectionRequest: request,
    onSelectionCommit: commit, onAnchorChange: anchor, onLocateResult: located }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  cleanups.push(() => { app.unmount(); host.remove() })
  await flush()
  const button = () => host.querySelector<HTMLButtonElement>('.office-preview__request')!
  return { host, state, request, button, commit, anchor, located }
}

describe('Office request-change entry', () => {
  it('locks the selected target without exiting annotation and refreshes after the guide moves the canvas', async () => {
    const panel = await mount('docx', true, undefined, true)
    sessions[0]!.onSelection(title)
    panel.state.selectionMode = false
    panel.state.selectionLocked = true
    await flush()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false, true)
    expect(refreshAnchor).toHaveBeenCalled()
    expect(panel.host.querySelector('.office-preview__guide')).toBeNull()
    setSelectionMode.mockClear()
    panel.host.querySelector('section')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(setSelectionMode).not.toHaveBeenCalled()
    clearSelection.mockClear()
    panel.host.querySelector<HTMLButtonElement>('.test-go-page')!.click()
    await flush()
    expect(clearSelection).not.toHaveBeenCalled()
    panel.state.selectionLocked = false
    await flush()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false)
  })
  it('uses the shared controlled entry and forwards geometry and locate status without a second button', async () => {
    const panel = await mount('pptx', true, undefined, true)
    expect(panel.button()).toBeNull()
    expect(setSelectionMode).toHaveBeenLastCalledWith(true)
    expect(panel.host.querySelector('.office-preview__hint')).toBeNull()
    const anchor = { rect: { left: 10, top: 20, width: 120, height: 30 },
      rects: [{ left: 10, top: 20, width: 120, height: 30 }],
      viewport: { left: 0, top: 0, width: 600, height: 400 }, visible: true }
    sessions[0]!.onSelectionCommitDetail?.({ selection: title, anchor })
    sessions[0]!.onAnchorChange?.(anchor)
    expect(panel.commit).toHaveBeenCalledExactlyOnceWith({ selection: title, anchor })
    expect(panel.anchor).toHaveBeenLastCalledWith(anchor)
    panel.state.selectionToLocate = { selection: title, revisionId: 'old-revision', stale: true, requestId: 'locate-1' }
    await flush()
    expect(locateSelection).toHaveBeenCalledWith(title, panel.state.selectionToLocate)
    expect(panel.located).toHaveBeenCalledWith({ status: 'location-only', anchor: null, requestId: 'locate-1' })
    panel.state.selectionMode = false
    await flush()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false)
  })
  it.each(['docx', 'pptx', 'xlsx'] as const)('restores %s page or sheet and zoom without reactivating selection', async format => {
    const panel = await mount(format, true, { page: 1, mode: 'manual', scale: 1.75, top: 88, left: 20, time: 0 })
    await flush(); await flush()
    expect(goTo).toHaveBeenCalledWith(1)
    expect(setScale).toHaveBeenCalledWith(1.75)
    expect(panel.host.querySelector<HTMLElement>('.office-preview__viewport')?.scrollTop).toBe(88)
    expect(panel.host.querySelector('.office-preview__guide')).toBeNull()
    expect(setSelectionMode).not.toHaveBeenCalledWith(true)
  })
  it.each(['docx', 'pptx', 'xlsx'] as const)('offers an explicit %s entry before selection without submitting an empty request', async format => {
    const h = await mount(format)
    expect(h.button().textContent).toContain(en.workbench.office.requestChange)
    expect(h.button().disabled).toBe(false)
    h.button().click(); await flush()
    expect(h.host.querySelector('.office-preview__guide')).not.toBeNull()
    expect(h.button().getAttribute('aria-pressed')).toBe('true')
    expect(setSelectionMode).toHaveBeenLastCalledWith(true)
    expect(document.activeElement).toBe(h.host.querySelector('section'))
    expect(h.request).not.toHaveBeenCalled()
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    h.button().dispatchEvent(event); await flush()
    expect(event.defaultPrevented).toBe(true)
    expect(h.host.querySelector('.office-preview__guide')).toBeNull()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false)
    expect(h.button().getAttribute('aria-pressed')).toBe('false')
  })

  it('opens the request on a completed selection without a second confirmation action', async () => {
    const h = await mount()
    h.button().click(); await flush()
    sessions[0]!.onSelection(title); await flush()
    expect(h.request).not.toHaveBeenCalled()
    expect(h.host.querySelector('.office-preview__guide')).not.toBeNull()
    expect(h.host.querySelector('.office-preview__selection-text')?.textContent).toContain('中文标题')
    expect(h.button().getAttribute('aria-pressed')).toBe('true')
    expect(h.host.querySelectorAll('.office-preview__request')).toHaveLength(1)
    expect(h.host.querySelector('.office-preview__submit')).toBeNull()
    sessions[0]!.onSelectionCommit?.(title)
    expect(h.request).toHaveBeenCalledExactlyOnceWith(title)
    expect(h.button().getAttribute('aria-pressed')).toBe('true')
    // A later deliberate selection may reopen the same target after cancellation.
    sessions[0]!.onSelectionCommit?.(title)
    expect(h.request).toHaveBeenCalledTimes(2)
    h.button().click(); await flush()
    expect(h.button().getAttribute('aria-pressed')).toBe('false')
    expect(h.host.querySelector('.office-preview__guide')).toBeNull()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false)
    expect(h.request).toHaveBeenCalledTimes(2)
    // A late viewer callback after exit cannot reintroduce an annotation target.
    sessions[0]!.onSelection(title); await flush()
    sessions[0]!.onSelectionCommit?.(title)
    h.button().click(); await flush()
    expect(h.host.querySelector('.office-preview__selection-text')).toBeNull()
    expect(h.request).toHaveBeenCalledTimes(2)
  })

  it('keeps the mode across pages but invalidates targets, and exits for new versions and suspension', async () => {
    const h = await mount()
    h.button().click(); await flush()
    sessions[0]!.onSelection(title)
    sessions[0]!.onLocation(1, 2); await flush()
    expect(h.request).not.toHaveBeenCalled()
    expect(h.host.querySelector('.office-preview__guide')).not.toBeNull()
    expect(h.host.querySelector('.office-preview__selection-text')).toBeNull()
    h.state.data = new ArrayBuffer(2); await flush()
    sessions[0]!.onSelection(title); await flush()
    sessions[0]!.onSelectionCommit?.(title)
    expect(h.host.querySelector('.office-preview__guide')).toBeNull()
    expect(h.button().getAttribute('aria-pressed')).toBe('false')
    h.button().click(); await flush()
    sessions[1]!.onSelection(title); await flush()
    h.state.suspended = true; await flush()
    sessions[1]!.onSelection(title); await flush()
    sessions[1]!.onSelectionCommit?.(title)
    expect(h.button().disabled).toBe(true)
    expect(h.host.querySelector('.office-preview__guide')).toBeNull()
    expect(h.request).not.toHaveBeenCalled()
  })

  it('does not advertise or submit modification when the capability is absent or revoked', async () => {
    const h = await mount('pptx', false)
    expect(h.button()).toBeNull()
    expect(h.host.querySelector('.office-preview__hint')?.textContent).toBe(en.workbench.office.readOnlyHint)
    h.state.annotationEnabled = true; await flush()
    h.button().click(); await flush()
    sessions[0]!.onSelection(title); await flush()
    h.state.annotationEnabled = false; await flush()
    sessions[0]!.onSelectionCommit?.(title)
    expect(h.button()).toBeNull()
    expect(h.host.querySelector('.office-preview__guide')).toBeNull()
    expect(setSelectionMode).toHaveBeenLastCalledWith(false)
    expect(h.request).not.toHaveBeenCalled()
  })

  it('consumes Escape while a target is selected, but leaves ordinary preview Escape alone', async () => {
    const h = await mount()
    h.button().click(); await flush()
    sessions[0]!.onSelection(title); await flush()
    const escape = () => {
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      h.host.querySelector('section')!.dispatchEvent(event)
      return event
    }
    expect(escape().defaultPrevented).toBe(true)
    await flush()
    expect(h.button().getAttribute('aria-pressed')).toBe('false')
    expect(h.host.querySelector('.office-preview__selection-text')).toBeNull()
    expect(escape().defaultPrevented).toBe(false)
    expect(h.request).not.toHaveBeenCalled()
  })

  it('does not redraw Word selection for a guide-only height change', async () => {
    await mount('docx')
    const notifySize = (width: number, height: number) => observeResize([{ contentRect: { width, height } } as ResizeObserverEntry], {} as ResizeObserver)
    notifySize(600, 800)
    notifySize(600, 740)
    expect(resize).toHaveBeenCalledTimes(1)
    notifySize(500, 740)
    expect(resize).toHaveBeenCalledTimes(2)
  })

  it('switches thumbnails to a compact rail and keeps annotation mode across page navigation', async () => {
    const h = await mount()
    expect(h.host.querySelector('.test-page-navigator')?.getAttribute('data-collapsed')).toBe('true')
    h.host.querySelector<HTMLButtonElement>('.office-preview__navigator-toggle')!.click(); await flush()
    expect(h.host.querySelector('.test-page-navigator')?.getAttribute('data-collapsed')).toBe('false')
    h.button().click(); await flush()
    sessions[0]!.onSelection(title)
    h.host.querySelector<HTMLButtonElement>('.test-go-page')!.click(); await flush()
    expect(goTo).toHaveBeenCalledWith(1)
    expect(clearSelection).toHaveBeenCalled()
    expect(h.button().getAttribute('aria-pressed')).toBe('true')
    expect(h.host.querySelector('.office-preview__selection-text')).toBeNull()
    expect(h.host.querySelector('.office-preview__position')?.textContent).toBe('2 / 2')
  })

  it('shows per-slide notes as plain text and drops old presentation callbacks on replacement', async () => {
    const h = await mount()
    expect(h.host.querySelector('.office-preview__notes')?.textContent).toContain('中文演讲者备注')
    expect(h.host.querySelector('.office-preview__notes script')).toBeNull()
    sessions[0]!.onLocation(1, 2); await flush()
    expect(h.host.querySelector('.office-preview__notes')?.textContent).toContain('第二页备注')
    sessions[0]!.onView?.({ mode: 'manual', scale: 1.25, width: 800, height: 450 }); await flush()
    expect(h.host.querySelector('.office-preview__zoom')?.textContent).toContain('125%')
    h.state.data = new ArrayBuffer(2); await flush()
    sessions[1]!.onView?.({ mode: 'page', scale: .8, width: 640, height: 360 })
    sessions[0]!.onView?.({ mode: 'manual', scale: 9, width: 999, height: 999 }); await flush()
    expect(h.host.querySelector<HTMLSelectElement>('.office-preview__zoom')?.value).toBe('page')
    expect(h.host.querySelector('.office-preview__zoom')?.textContent).not.toContain('900%')
  })

  it('keeps spreadsheet navigation and geometry separate from paged reading', async () => {
    const h = await mount('xlsx')
    expect(h.host.querySelector('.test-page-navigator')).toBeNull()
    expect(h.host.querySelector('.office-preview__notes')).toBeNull()
    expect(h.host.querySelector('.office-preview__viewport--paged')).toBeNull()
    expect(h.host.querySelector('.office-preview__zoom')).toBeNull()
  })
})
