// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useComposerToolbarFit } from './useComposerToolbarFit'

const apps: App[] = []
let resizeCallbacks: ResizeObserverCallback[]
let frames: Map<number, FrameRequestCallback>
let disconnect: ReturnType<typeof vi.fn>
let nextFrame = 0

beforeEach(() => {
  frames = new Map()
  resizeCallbacks = []
  disconnect = vi.fn()
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback)
    return nextFrame
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resizeCallbacks.push(callback) }
    observe = vi.fn()
    disconnect = disconnect
  })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const panel = this.closest<HTMLElement>('.test-panel')
    const width = Number(panel?.dataset.width || 0)
    const density = this.closest<HTMLElement>('.chat-input-footer')?.dataset.composerDensity || 'wide'
    const left = Number(panel?.dataset[`${density}Left`] || 0)
    const right = Number(panel?.dataset[`${density}Right`] || 0)
    const measured = this.classList.contains('chat-input-actions--left') ? left
      : this.classList.contains('chat-input-actions--right') ? right : width
    return { x: 0, y: 0, left: 0, top: 0, width: measured, height: 34, bottom: 34, right: measured, toJSON() {} }
  })
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

function flushFrame() {
  const pending = [...frames.values()]
  frames.clear()
  pending.forEach(callback => callback(0))
}

async function mount(width = 800) {
  const host = document.createElement('div')
  document.body.append(host)
  const label = ref('Fixed model')
  const clicked = vi.fn()
  const app = createApp(defineComponent({ setup() {
    const panel = ref<HTMLElement | null>(null)
    const toolbar = ref<HTMLElement | null>(null)
    useComposerToolbarFit(toolbar, panel)
    return () => h('div', { ref: panel, class: 'test-panel', 'data-width': width,
      'data-wide-left': 260, 'data-wide-right': 300,
      'data-compact-left': 90, 'data-compact-right': 300,
      'data-icons-left': 90, 'data-icons-right': 70,
    }, [h('div', { ref: toolbar, class: 'chat-input-footer' }, [
      h('div', { class: 'chat-input-actions chat-input-actions--left' }, [
        h('div', { class: 'chat-settings-anchor' }, [
          h('button', { id: 'original-trigger', onClick: clicked, 'aria-expanded': 'true' }, 'Access'),
          h('div', { id: 'original-menu', role: 'dialog' }, 'Open menu'),
        ]),
      ]),
      h('div', { class: 'chat-input-actions chat-input-actions--right' }, [
        h('button', { title: label.value }, label.value),
      ]),
    ])])
  } }))
  apps.push(app)
  app.mount(host)
  await nextTick()
  return { host, panel: host.querySelector<HTMLElement>('.test-panel')!,
    toolbar: host.querySelector<HTMLElement>('.chat-input-footer')!, label, clicked, app }
}

function resize(panel: HTMLElement, width: number) {
  panel.dataset.width = String(width)
  resizeCallbacks.forEach(callback => callback([], {} as ResizeObserver))
  flushFrame()
}

describe('shared composer toolbar fit', () => {
  it('uses each panel width rather than the window and restores wide mode after widening', async () => {
    const main = await mount(800)
    const side = await mount(360)
    expect(main.toolbar.dataset.composerDensity).toBe('wide')
    expect(side.toolbar.dataset.composerDensity).toBe('icons')
    resize(main.panel, 520)
    expect(main.toolbar.dataset.composerDensity).toBe('compact')
    expect(side.toolbar.dataset.composerDensity).toBe('icons')
    resize(main.panel, 480)
    expect(main.toolbar.dataset.composerDensity).toBe('icons')
    resize(main.panel, 800)
    expect(main.toolbar.dataset.composerDensity).toBe('wide')
  })

  it('fits long labels above the narrow breakpoint before falling back to icons', async () => {
    const test = await mount(650)
    test.panel.dataset.wideLeft = '440'
    resize(test.panel, 650)
    expect(test.toolbar.dataset.composerDensity).toBe('compact')
    test.panel.dataset.compactRight = '620'
    resize(test.panel, 650)
    expect(test.toolbar.dataset.composerDensity).toBe('icons')
  })

  it('preserves focused controls, the open menu, and event handlers through fit changes', async () => {
    const test = await mount()
    const button = test.host.querySelector<HTMLButtonElement>('#original-trigger')!
    const menu = test.host.querySelector('#original-menu')!
    button.focus()
    resize(test.panel, 360)
    resize(test.panel, 650)
    expect(test.host.querySelector('#original-trigger')).toBe(button)
    expect(test.host.querySelector('#original-menu')).toBe(menu)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(button)
    button.click()
    expect(test.clicked).toHaveBeenCalledOnce()
    expect(document.querySelectorAll('#original-trigger')).toHaveLength(1)
    expect(document.querySelectorAll('.chat-input-footer')).toHaveLength(1)
  })

  it('never exposes measurement copies or duplicates a live menu while measuring', async () => {
    const test = await mount()
    const append = vi.spyOn(test.panel, 'append').mockImplementation(function (...nodes) {
      const probe = nodes[0] as HTMLElement
      expect(probe.inert).toBe(true)
      expect(probe.getAttribute('aria-hidden')).toBe('true')
      expect(probe.querySelector('[id]')).toBeNull()
      expect(probe.querySelector('[role="dialog"]')).toBeNull()
      HTMLElement.prototype.append.apply(test.panel, nodes)
    })
    resize(test.panel, 650)
    expect(append).toHaveBeenCalledOnce()
  })

  it('remeasures content changes, coalesces observer work and skips hidden panels', async () => {
    const test = await mount(650)
    test.panel.dataset.wideRight = '600'
    test.label.value = 'Sehr langer Modellname für die automatische Auswahl'
    await nextTick()
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(frames.size).toBe(1)
    resizeCallbacks.forEach(callback => { callback([], {} as ResizeObserver); callback([], {} as ResizeObserver) })
    expect(frames.size).toBe(1)
    flushFrame()
    expect(test.toolbar.dataset.composerDensity).toBe('compact')
    resize(test.panel, 0)
    expect(test.toolbar.dataset.composerDensity).toBe('compact')
    test.app.unmount()
    expect(disconnect).toHaveBeenCalledOnce()
    resizeCallbacks.forEach(callback => callback([], {} as ResizeObserver))
    expect(frames.size).toBe(0)
  })
})
