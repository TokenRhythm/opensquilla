// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createApp, Fragment, h, nextTick, ref, type App } from 'vue'
import type { SidebarDisplayRow } from '@/utils/sidebarDisplayProjection'
import { useSidebarVirtualizer } from './useSidebarVirtualizer'

const apps: App[] = []
function row(index: number): SidebarDisplayRow {
  return {
    key: `task-${index}`, title: `Task ${index}`, rowKind: 'session', sessionKind: 'chat',
    effectiveAgentId: 'main', agentName: 'Main', depth: 0, runStatus: 'idle', runLabel: 'Idle',
    taskAttention: 'none', updatedAt: index, hasContractGaps: false,
    displayZone: 'recents', displayFamily: 'chats', displayProjectName: '',
  }
}
type Block = { key: string; rows: SidebarDisplayRow[]; collapsed: boolean }
function recordResizeObservers() {
  const observers: Array<{ callback: ResizeObserverCallback; elements: Set<Element> }> = []
  vi.stubGlobal('ResizeObserver', class {
    record: (typeof observers)[number]
    constructor(callback: ResizeObserverCallback) {
      this.record = { callback, elements: new Set() }
      observers.push(this.record)
    }
    observe(element: Element) { this.record.elements.add(element) }
    unobserve(element: Element) { this.record.elements.delete(element) }
    disconnect() { this.record.elements.clear() }
  })
  return observers
}
async function flush() {
  await nextTick()
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  await nextTick()
}
async function mount(count = 200) {
  const blocks = ref<Block[]>([{ key: 'recents', rows: Array.from({ length: count }, (_, index) => row(index)), collapsed: false }])
  const retained = ref<string[]>([])
  const focused = ref('')
  const element = document.createElement('div')
  const host = document.createElement('div')
  element.append(host)
  document.body.append(element)
  Object.defineProperties(element, {
    offsetHeight: { value: 176, configurable: true },
    offsetWidth: { value: 260, configurable: true },
    clientHeight: { value: 176, configurable: true },
    scrollHeight: { get: () => blocks.value.reduce((sum, block) => sum + 44 + (block.collapsed ? 0 : block.rows.length * 44), 0) },
    scrollTop: { value: 0, writable: true, configurable: true },
  })
  element.scrollTo = vi.fn(options => {
    element.scrollTop = (options as ScrollToOptions).top || 0
    element.dispatchEvent(new Event('scroll'))
  })
  let api!: ReturnType<typeof useSidebarVirtualizer<Block>>
  const app = createApp({ setup() {
    api = useSidebarVirtualizer(ref(element), computed(() => blocks.value), computed(() => retained.value), focused)
    return () => api.renderedBlocks.value.map(section => h('section', { key: section.block.key }, [
      h('div', { 'data-index': section.headingIndex, ref: (el: unknown) => api.measureElement(el as Element | null) }),
      ...section.rows.map(entry => h(Fragment, { key: entry.row.key }, [
        entry.gapBefore ? h('div', { 'data-gap': '', style: { height: `${entry.gapBefore}px` } }) : null,
        h('div', {
          'data-index': entry.index, 'data-key': entry.row.key,
          ref: (el: unknown) => api.measureElement(el as Element | null),
        }),
      ])),
      section.gapAfter ? h('div', { 'data-gap': '', style: { height: `${section.gapAfter}px` } }) : null,
    ]))
  } })
  app.mount(host)
  apps.push(app)
  await flush()
  return { api, blocks, retained, focused, element, keys: () => api.renderedBlocks.value.flatMap(section => section.rows.map(entry => entry.row.key)) }
}
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return new DOMRect(0, 0, 260, this.hasAttribute('data-index') ? 44 : 176)
  })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(44)
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('sidebar TanStack window', () => {
  it('keeps short lists complete and long lists bounded', async () => {
    const short = await mount(24)
    expect(short.api.virtualized.value).toBe(false)
    expect(short.keys()).toHaveLength(24)
    const long = await mount(3000)
    expect(long.api.virtualized.value).toBe(true)
    expect(long.keys().length).toBeLessThan(20)
    expect(long.api.renderedBlocks.value[0]!.gapAfter).toBeGreaterThan(100_000)
    long.element.scrollTop = 44_000
    long.element.dispatchEvent(new Event('scroll'))
    await flush()
    expect(long.keys()).toContain('task-1000')
    expect(long.keys()).not.toContain('task-0')
    expect(long.keys().length).toBeLessThan(20)
  })

  it('retains only interaction leases and the focused row’s native Tab neighbors', async () => {
    const state = await mount()
    state.retained.value = ['task-150', 'task-180']
    state.focused.value = 'row:task-100'
    await flush()
    expect(state.keys()).toEqual(expect.arrayContaining(['task-99', 'task-100', 'task-101', 'task-150', 'task-180']))
    expect(state.keys().length).toBeLessThan(25)
    state.retained.value = []
    state.focused.value = ''
    await flush()
    expect(state.keys()).not.toContain('task-150')
    expect(state.keys()).not.toContain('task-100')
  })

  it('does not clamp the scroll position while replacing a window at scroll end', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const state = await mount(3000)
    state.element.scrollTop = 2800 * 44
    state.element.dispatchEvent(new Event('scroll'))
    await flush()

    let layoutReads = 0
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      if (this.hasAttribute('data-index') && state.element.contains(this)) {
        layoutReads++
        // A layout read clamps native scrolling to the DOM's current extent.
        // Happy DOM has no layout, so model that browser effect over the actual
        // Vue-rendered headings, rows and normal-flow spacers.
        const extent = [...state.element.querySelectorAll<HTMLElement>('[data-index], [data-gap]')]
          .reduce((height, node) => height + (node.hasAttribute('data-index') ? 44 : Number.parseFloat(node.style.height)), 0)
        state.element.scrollTop = Math.min(state.element.scrollTop, Math.max(0, extent - state.element.clientHeight))
      }
      return 44
    })

    const bottom = state.element.scrollHeight - state.element.clientHeight
    state.element.scrollTop = bottom
    // The existing scroll-end callback can see this new position before its
    // native scroll event, so the virtualizer replaces rows while not scrolling.
    await vi.runOnlyPendingTimersAsync()
    await flush()

    expect(layoutReads).toBeGreaterThan(0)
    expect(state.element.scrollTop).toBe(bottom)
    expect(state.keys()).toContain('task-2999')
  })

  it('keeps group headers and accounts for disjoint window gaps exactly once', async () => {
    const state = await mount()
    state.blocks.value.push({ key: 'projects', collapsed: false, rows: Array.from({ length: 200 }, (_, index) => row(index + 200)) })
    state.retained.value = ['task-350']
    await flush()
    expect(state.api.renderedBlocks.value).toHaveLength(2)
    const extent = state.api.renderedBlocks.value.reduce((sum, block) => (
      sum + 44 + block.gapAfter + block.rows.reduce((height, entry) => height + entry.gapBefore + 44, 0)
    ), 0)
    expect(extent).toBe(402 * 44)
    state.blocks.value[0]!.collapsed = true
    await flush()
    expect(state.api.renderedBlocks.value[0]!.rows).toHaveLength(0)
    expect(state.api.renderedBlocks.value[0]!.gapAfter).toBe(0)
    expect(state.api.hasRow('task-100')).toBe(false)
    expect(state.api.hasRow('task-350')).toBe(true)
  })

  it('preserves the reading anchor on a drawer width change during backward scrolling', async () => {
    const observers = recordResizeObservers()
    const state = await mount()
    state.element.scrollTop = 100 * 44
    state.element.dispatchEvent(new Event('scroll'))
    await flush()
    state.element.scrollTop = 90 * 44 + 12
    state.element.dispatchEvent(new Event('scroll'))
    await flush()

    // Native observers also report first measurements for newly rendered
    // overscan rows while scroll events are still in flight.
    for (const observer of observers) {
      const entries = [...observer.elements].filter(element => element.hasAttribute('data-index')).map(element => ({
        target: element, borderBoxSize: [{ blockSize: 44, inlineSize: 260 }],
      })) as unknown as ResizeObserverEntry[]
      if (entries.length) observer.callback(entries, {} as ResizeObserver)
    }

    const before = state.element.scrollTop
    const firstVisibleIndex = Math.floor(before / 44)
    const measured = [...state.element.querySelectorAll<HTMLElement>('[data-index]')]
    const sizeChanges = measured.map(element => ({
      element, index: Number(element.dataset.index), delta: element.dataset.index === '0' ? 4 : 6,
    }))
    const expectedAdjustment = sizeChanges.filter(item => item.index < firstVisibleIndex)
      .reduce((sum, item) => sum + item.delta, 0)
    const deliverWidth = (width: number) => {
      for (const observer of observers) {
        if (observer.elements.has(state.element)) observer.callback([{
          target: state.element, borderBoxSize: [{ blockSize: 176, inlineSize: width }],
        }] as unknown as ResizeObserverEntry[], {} as ResizeObserver)
      }
    }
    const deliverResize = () => {
      for (const observer of observers) {
        const entries = sizeChanges.filter(item => observer.elements.has(item.element)).map(item => ({
          target: item.element, borderBoxSize: [{ blockSize: 44 + item.delta, inlineSize: 272 }],
        })) as unknown as ResizeObserverEntry[]
        if (entries.length) observer.callback(entries, {} as ResizeObserver)
      }
    }
    deliverWidth(272)
    // Container and item observers can have a microtask checkpoint between
    // deliveries. A nextTick-only restore would consume the anchor too early.
    await nextTick()
    deliverResize()
    await flush()
    // Only fully offscreen rows compensate. The partially visible row and the
    // rows below it may grow without moving the reader's current position.
    expect(expectedAdjustment).toBeGreaterThan(0)
    expect(state.element.scrollTop).toBe(before + expectedAdjustment)
    vi.mocked(state.element.scrollTo).mockClear()
    deliverWidth(272)
    deliverResize()
    await flush()
    expect(state.element.scrollTop).toBe(before + expectedAdjustment)
    expect(state.element.scrollTo).not.toHaveBeenCalled()

    // A wheel gesture takes precedence over a pending layout restoration.
    deliverWidth(244)
    state.element.dispatchEvent(new WheelEvent('wheel'))
    state.element.scrollTop += 20
    state.element.dispatchEvent(new Event('scroll'))
    const userPosition = state.element.scrollTop
    await flush()
    expect(state.element.scrollTop).toBe(userPosition)
    expect(state.element.scrollTo).not.toHaveBeenCalled()

    const cancelFrame = vi.spyOn(window, 'cancelAnimationFrame')
    deliverWidth(272)
    apps.pop()!.unmount()
    await flush()
    expect(cancelFrame).toHaveBeenCalled()
    expect(state.element.scrollTo).not.toHaveBeenCalled()
  })

  it('lets an explicit reveal replace a pending width-change reading anchor', async () => {
    const observers = recordResizeObservers()
    const state = await mount()
    // The old heading remains rendered even after navigating far away, so the
    // pending frame must be cancelled rather than relying on a missing anchor.
    for (const observer of observers) {
      if (observer.elements.has(state.element)) observer.callback([{
        target: state.element, borderBoxSize: [{ blockSize: 176, inlineSize: 272 }],
      }] as unknown as ResizeObserverEntry[], {} as ResizeObserver)
    }
    await state.api.revealRow('task-175')
    const revealedPosition = state.element.scrollTop
    expect(revealedPosition).toBeGreaterThan(7000)
    await flush()
    expect(state.element.scrollTop).toBe(revealedPosition)
    expect(state.keys()).toContain('task-175')
  })

  it('reveals by stable key and ignores a removed destination', async () => {
    const state = await mount()
    await state.api.revealRow('task-175')
    await flush()
    expect(state.keys()).toContain('task-175')
    const before = state.element.scrollTop
    await state.api.revealRow('missing')
    expect(state.element.scrollTop).toBe(before)
  })

  it('does not retain a deleted leased row or render a duplicate after moving groups', async () => {
    const state = await mount()
    state.retained.value = ['task-150']
    const moved = state.blocks.value[0]!.rows.splice(150, 1)[0]!
    state.blocks.value.unshift({ key: 'pinned', rows: [moved], collapsed: false })
    await flush()
    expect(state.keys().filter(key => key === 'task-150')).toHaveLength(1)
    state.blocks.value.shift()
    await flush()
    expect(state.keys()).not.toContain('task-150')
  })

  it('crosses the small-list threshold in both directions without stale gaps', async () => {
    const state = await mount(99)
    expect(state.keys()).toHaveLength(99)
    state.blocks.value[0]!.rows.push(row(99))
    await flush()
    expect(state.api.virtualized.value).toBe(true)
    expect(state.keys().length).toBeLessThan(20)
    state.blocks.value[0]!.rows.pop()
    await flush()
    expect(state.api.virtualized.value).toBe(false)
    expect(state.keys()).toHaveLength(99)
    expect(state.api.renderedBlocks.value[0]!.gapAfter).toBe(0)
    state.blocks.value = []
    await flush()
    expect(state.api.renderedBlocks.value).toEqual([])
  })
})
