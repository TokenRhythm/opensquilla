// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useSystemHeaderLayout } from './useSystemHeaderLayout'

let measuredWidth = 1100
let resizeCallback: ResizeObserverCallback | null = null
let pointerChange: EventListener | null = null
let coarsePointer = false
let nextFrameId = 0
let frameCallbacks = new Map<number, FrameRequestCallback>()
const mountedApps: App[] = []

function rect(width: number): DOMRect {
  return {
    x: 0,
    y: 0,
    top: 0,
    right: width,
    bottom: 49,
    left: 0,
    width,
    height: 49,
    toJSON: () => ({}),
  } as DOMRect
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    value: width,
  })
}

function flushFrames() {
  const pending = [...frameCallbacks.values()]
  frameCallbacks = new Map()
  pending.forEach(callback => callback(performance.now()))
}

async function notifyResize() {
  resizeCallback?.([], {} as ResizeObserver)
  await nextTick()
  flushFrames()
  await nextTick()
}

async function mountHarness(options: {
  active?: boolean
  pressureCount?: number
} = {}) {
  const active = ref(options.active ?? true)
  const pressureCount = ref(options.pressureCount ?? 0)
  let layout!: ReturnType<typeof useSystemHeaderLayout>

  const Harness = defineComponent({
    setup() {
      const target = ref<HTMLElement | null>(null)
      layout = useSystemHeaderLayout({ target, active, pressureCount })
      return () => h('header', { ref: target })
    },
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Harness)
  mountedApps.push(app)
  app.mount(host)
  await nextTick()
  return { active, pressureCount, layout }
}

beforeEach(() => {
  measuredWidth = 1100
  resizeCallback = null
  pointerChange = null
  coarsePointer = false
  nextFrameId = 0
  frameCallbacks = new Map()
  setViewportWidth(1440)

  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect')
    .mockImplementation(() => rect(measuredWidth))
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) {
      resizeCallback = callback
    }

    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
    const id = ++nextFrameId
    frameCallbacks.set(id, callback)
    return id
  })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => {
    frameCallbacks.delete(id)
  })
  vi.spyOn(window, 'matchMedia').mockImplementation(() => ({
    get matches() { return coarsePointer },
    media: '(pointer: coarse)',
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
      pointerChange = typeof listener === 'function'
        ? listener
        : event => listener.handleEvent(event)
    },
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } as MediaQueryList))
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useSystemHeaderLayout', () => {
  it('uses the full observed topbar width and pressure categories', async () => {
    const { layout, pressureCount } = await mountHarness()
    expect(layout.value).toBe('wide')

    pressureCount.value = 1
    await nextTick()
    flushFrames()
    expect(layout.value).toBe('wide')

    pressureCount.value = 2
    await nextTick()
    flushFrames()
    expect(layout.value).toBe('compact')

    pressureCount.value = 1
    await nextTick()
    flushFrames()
    expect(layout.value).toBe('wide')
  })

  it('preserves compact/tight hysteresis across resize observations', async () => {
    measuredWidth = 450
    const { layout } = await mountHarness()
    expect(layout.value).toBe('compact')

    measuredWidth = 383
    await notifyResize()
    expect(layout.value).toBe('tight')

    measuredWidth = 415
    await notifyResize()
    expect(layout.value).toBe('tight')

    measuredWidth = 416
    await notifyResize()
    expect(layout.value).toBe('compact')
  })

  it('caps wide on mobile and coarse pointers while still allowing tight', async () => {
    measuredWidth = 1400
    setViewportWidth(700)
    const { layout } = await mountHarness()
    expect(layout.value).toBe('compact')

    setViewportWidth(1440)
    coarsePointer = true
    pointerChange?.(new Event('change'))
    await nextTick()
    flushFrames()
    expect(layout.value).toBe('compact')

    measuredWidth = 300
    await notifyResize()
    expect(layout.value).toBe('tight')
  })

  it('coalesces observation, viewport, pointer, and pressure changes into one frame', async () => {
    const { pressureCount } = await mountHarness()
    const requestFrame = vi.mocked(window.requestAnimationFrame)
    requestFrame.mockClear()

    resizeCallback?.([], {} as ResizeObserver)
    resizeCallback?.([], {} as ResizeObserver)
    window.dispatchEvent(new Event('resize'))
    pointerChange?.(new Event('change'))
    pressureCount.value = 2
    await nextTick()

    expect(requestFrame).toHaveBeenCalledTimes(1)
    flushFrames()
  })

  it('resets hysteresis after a non-chat interval', async () => {
    const { active, layout } = await mountHarness()
    measuredWidth = 944
    await notifyResize()
    // A prior wide state remains wide inside its hysteresis band.
    expect(layout.value).toBe('wide')

    active.value = false
    await nextTick()
    measuredWidth = 944
    active.value = true
    await nextTick()
    await nextTick()
    flushFrames()
    expect(layout.value).toBe('compact')
  })
})
