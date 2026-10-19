// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, watchEffect } from 'vue'
import { useToastPlacement } from './useToastPlacement'

const cleanups: (() => void)[] = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

function rect(left: number, top: number, width: number, height: number) {
  return { left, top, width, height, right: left + width, bottom: top + height } as DOMRect
}

function harness(initial: DOMRect[]) {
  const host = document.createElement('div')
  document.body.append(host)
  const surfaces = initial.map(bounds => {
    const element = document.createElement('div')
    element.className = 'protected-input'
    document.body.append(element)
    const state = { bounds, element }
    element.getBoundingClientRect = () => state.bounds
    return state
  })
  let baseBottom = 16
  let contentHeight = 60
  const oldHeight = window.innerHeight
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
  cleanups.push(() => Object.defineProperty(window, 'innerHeight', { configurable: true, value: oldHeight }))
  const offset = () => Number.parseFloat(host.style.getPropertyValue('--toast-avoidance-offset')) || 0
  const bottom = () => window.innerHeight - baseBottom - offset()
  host.getBoundingClientRect = () => rect(700, bottom() - Math.min(contentHeight, 400), 284, Math.min(contentHeight, 400))
  Object.defineProperty(host, 'scrollHeight', { get: () => contentHeight })
  const getStyle = getComputedStyle
  vi.stubGlobal('getComputedStyle', (element: Element) => element === host ? {
    bottom: `${baseBottom + offset()}px`, rowGap: '8px',
    getPropertyValue: (key: string) => host.style.getPropertyValue(key),
  } as CSSStyleDeclaration : getStyle(element))
  let frameId = 0
  const frames = new Map<number, FrameRequestCallback>()
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++frameId, callback)
    return frameId
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  const observers: { run: () => void; observe: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = []
  vi.stubGlobal('ResizeObserver', class {
    observe = vi.fn()
    unobserve = vi.fn()
    disconnect = vi.fn()
    constructor(callback: () => void) { observers.push({ run: callback, observe: this.observe, disconnect: this.disconnect }) }
  })
  const mutations: { run: (target?: Node) => void; disconnect: ReturnType<typeof vi.fn> }[] = []
  vi.stubGlobal('MutationObserver', class {
    observe = vi.fn()
    disconnect = vi.fn()
    constructor(callback: (records: MutationRecord[]) => void) {
      mutations.push({ run: (target = document.body) => callback([{ target } as MutationRecord]), disconnect: this.disconnect })
    }
  })
  const visible = ref(false)
  const selector = ref<string | undefined>('.protected-input')
  const layout = ref(false)
  const scope = effectScope()
  scope.run(() => {
    const placement = useToastPlacement({ host: () => host, visible: () => visible.value,
      avoidSelector: () => selector.value, layoutKey: () => layout.value })
    watchEffect(() => host.style.setProperty('--toast-avoidance-offset', placement.value['--toast-avoidance-offset']))
  })
  cleanups.push(() => scope.stop())
  const flush = async () => {
    await nextTick()
    const batch = [...frames.values()]
    frames.clear()
    for (const callback of batch) callback(0)
    await nextTick()
  }
  return { host, surfaces, visible, selector, layout, scope, observers, mutations, frames, bottom, offset, flush,
    setBaseBottom: (value: number) => { baseBottom = value }, setContentHeight: (value: number) => { contentHeight = value } }
}

describe('toast placement around interactive input surfaces', () => {
  it('retains the bottom corner when no protected input intersects the stack', async () => {
    const h = harness([rect(50, 680, 400, 100), rect(700, 300, 284, 150)])
    h.visible.value = true
    await h.flush()
    expect(h.offset()).toBe(0)
    expect(h.bottom()).toBe(784)
  })

  it('moves above an intersecting composer and follows its expansion without growing the offset each frame', async () => {
    const h = harness([rect(300, 650, 650, 130)])
    h.visible.value = true
    await h.flush()
    expect(h.bottom()).toBe(642)
    h.surfaces[0]!.bounds = rect(300, 500, 650, 280)
    h.observers[0]!.run()
    h.observers[0]!.run()
    expect(h.frames.size).toBe(1)
    await h.flush()
    expect(h.bottom()).toBe(492)
    h.observers[0]!.run()
    await h.flush()
    expect(h.bottom()).toBe(492)
  })

  it('preserves tab-bar and keyboard clearance and stops avoiding a removed composer', async () => {
    const h = harness([rect(300, 650, 650, 90)])
    h.setBaseBottom(72)
    h.visible.value = true
    await h.flush()
    expect(h.bottom()).toBe(642)
    h.setBaseBottom(16)
    h.layout.value = true
    await h.flush()
    expect(h.bottom()).toBe(642)
    h.surfaces[0]!.element.remove()
    h.mutations[h.mutations.length - 1]!.run()
    await h.flush()
    expect(h.bottom()).toBe(784)
  })

  it('responds to layout movement without a size change and ignores its own placement mutation', async () => {
    const h = harness([rect(50, 650, 400, 130)])
    h.visible.value = true
    await h.flush()
    expect(h.offset()).toBe(0)
    h.surfaces[0]!.bounds = rect(600, 650, 400, 130)
    h.mutations[0]!.run()
    await h.flush()
    expect(h.bottom()).toBe(642)
    h.mutations[0]!.run(h.host)
    expect(h.frames.size).toBe(0)
    h.surfaces[0]!.bounds = rect(50, 650, 400, 130)
    window.dispatchEvent(new Event('transitionend'))
    await h.flush()
    expect(h.offset()).toBe(0)
  })

  it('uses the full stack for collision detection and avoids a second input in its new position', async () => {
    const h = harness([rect(300, 650, 650, 130), rect(700, 350, 200, 100)])
    h.setContentHeight(700)
    h.visible.value = true
    await h.flush()
    expect(h.bottom()).toBe(342)
    h.observers[0]!.run()
    await h.flush()
    expect(h.bottom()).toBe(342)
  })

  it('has no observers while empty and releases observers and queued work after dismissal or unmount', async () => {
    const h = harness([rect(300, 650, 650, 130)])
    expect(h.observers).toHaveLength(0)
    expect(h.mutations).toHaveLength(0)
    h.visible.value = true
    await h.flush()
    expect(h.observers[0]!.observe).toHaveBeenCalledTimes(2)
    h.observers[0]!.run()
    h.visible.value = false
    await nextTick()
    expect(h.frames.size).toBe(0)
    expect(h.observers[0]!.disconnect).toHaveBeenCalledOnce()
    expect(h.mutations[0]!.disconnect).toHaveBeenCalledOnce()
    expect(h.bottom()).toBe(642)
    h.visible.value = true
    await h.flush()
    h.observers[1]!.run()
    h.scope.stop()
    expect(h.frames.size).toBe(0)
    expect(h.observers[1]!.disconnect).toHaveBeenCalledOnce()
    expect(h.mutations[1]!.disconnect).toHaveBeenCalledOnce()
  })
})
