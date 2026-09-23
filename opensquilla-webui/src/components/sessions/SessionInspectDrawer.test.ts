// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, ref } from 'vue'
import type { App } from 'vue'
import { SESSION_INSPECTION_KEY } from '@/modules/sessionInspection'
import { TURN_COMMANDS_KEY } from '@/modules/turnCommands'
import type { SessionItem } from '@/composables/useSessions'
import * as virtualizerLayout from '@/utils/virtualizerLayout'

const state = vi.hoisted(() => ({ current: null as any, virtualizer: null as any }))
vi.mock('@tanstack/vue-virtual', async importOriginal => {
  const actual = await importOriginal<typeof import('@tanstack/vue-virtual')>()
  return { ...actual, useVirtualizer: (...args: Parameters<typeof actual.useVirtualizer>) => {
    const instance = actual.useVirtualizer(...args)
    state.virtualizer = instance
    return instance
  } }
})
vi.mock('@/composables/sessions/useSessionInspect', () => ({
  useSessionInspect: () => state.current,
  abortInspectedSession: vi.fn(),
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/composables/chat/useChatTextRendering', () => ({
  useChatTextRendering: () => ({
    renderMarkdown: (text: string) => text,
    stripDirectiveTags: (text: string) => text,
    stripTimePrefix: (text: string) => text,
  }),
}))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm: vi.fn() }) }))
vi.mock('@/components/run/runTrace', () => ({ nodeStepsFromSessionReadMessage: () => [] }))
vi.mock('./sessionDisplay', () => ({
  sessionRelTime: () => 'now', sessionStatusBadge: () => null, sessionSurfaceIcon: () => 'chat',
}))
vi.mock('@/components/Icon.vue', () => ({ default: defineComponent({ setup: () => () => h('span') }) }))
vi.mock('@/components/LoadingSpinner.vue', () => ({ default: defineComponent({ setup: () => () => h('span') }) }))
vi.mock('@/components/run/RunTrace.vue', () => ({ default: defineComponent({ setup: () => () => h('div') }) }))
vi.mock('@/components/HistoryLoadSentinel.vue', () => ({
  default: defineComponent({
    emits: ['load-earlier', 'retry'],
    setup(_, { emit }) {
      return () => h('button', { class: 'load-earlier', onClick: () => emit('load-earlier') }, 'Load earlier')
    },
  }),
}))

import SessionInspectDrawer from './SessionInspectDrawer.vue'

const apps: App[] = []
let resizeObservers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = []
let viewportWidth = 528
let rowHeight = 96
let viewportHeight = 480

function messages(from: number, count: number) {
  return Array.from({ length: count }, (_, index) => ({
    messageId: `message-${from + index}`, role: 'assistant', text: `Synthetic message ${from + index}`,
  }))
}

async function flush() {
  for (let i = 0; i < 8; i++) await nextTick()
}

function resize(target: Element) {
  for (const observer of resizeObservers) {
    if (observer.targets.has(target)) observer.callback([{ target } as ResizeObserverEntry], {} as ResizeObserver)
  }
}

async function mountDrawer() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const props = reactive({
    open: false,
    item: { key: 'synthetic-session', title: 'Synthetic session', messageCount: 200, runStatus: 'idle' } as SessionItem,
    agentName: 'Synthetic agent',
  })
  const app = createApp({ setup: () => () => h(SessionInspectDrawer, props) })
  app.provide(SESSION_INSPECTION_KEY, {} as never)
  app.provide(TURN_COMMANDS_KEY, {} as never)
  app.mount(el)
  apps.push(app)
  props.open = true
  await flush()
  await vi.advanceTimersByTimeAsync(100)
  await flush()
  return { el, props, body: el.querySelector<HTMLElement>('.inspect-body')! }
}

beforeEach(() => {
  vi.useFakeTimers()
  viewportWidth = 528
  rowHeight = 96
  viewportHeight = 480
  resizeObservers = []
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    constructor(callback: ResizeObserverCallback) { resizeObservers.push({ callback, targets: this.targets }) }
    observe(target: Element) { this.targets.add(target) }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(() => viewportWidth)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('inspect-body') ? viewportHeight : this.classList.contains('inspect-msg') ? rowHeight : 100
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('inspect-body') ? viewportHeight : this.offsetHeight
  })
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    const window = this.querySelector<HTMLElement>('.inspect-window')
    return window ? Number.parseFloat(window.style.height || '0') + 100 : this.offsetHeight
  })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return { x: 0, y: 0, top: 0, left: 0, right: viewportWidth, bottom: this.offsetHeight,
      width: viewportWidth, height: this.offsetHeight, toJSON: () => ({}) }
  })
  vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(function (this: HTMLElement, options?: ScrollToOptions | number) {
    if (!options || typeof options !== 'object') return
    this.scrollTop = Math.max(0, Math.min(options.top ?? 0, this.scrollHeight - this.clientHeight))
    this.dispatchEvent(new Event('scroll'))
  })
  const transcript = ref(messages(100, 200))
  state.current = {
    preview: ref(null), messages: transcript, hasEarlier: ref(true), loading: ref(false),
    loadingEarlier: ref(false), loadEarlierError: ref(false), transcriptError: ref(false),
    canonicalAvailable: ref(true), canonicalComplete: ref(false), oldestCursor: ref('100'),
    load: vi.fn(async () => {}),
    loadEarlier: vi.fn(async () => { transcript.value = [...messages(50, 50), ...transcript.value] }),
    retryHistory: vi.fn(async () => {}), reset: vi.fn(),
  }
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SessionInspectDrawer virtual transcript', () => {
  it('renders the latest measured rows with a bounded DOM and persistent history header', async () => {
    const { el } = await mountDrawer()
    expect(state.current.load).toHaveBeenCalledWith('synthetic-session')
    expect(el.querySelectorAll('.inspect-msg').length).toBeLessThan(25)
    expect(el.querySelector('.inspect-msg:last-child')?.textContent).toContain('Synthetic message 299')
    expect(el.querySelectorAll('.load-earlier')).toHaveLength(1)
  })

  it('keeps the same reading row in place while older transcript rows are prepended', async () => {
    const { el, body } = await mountDrawer()
    body.scrollTo({ top: 1_600 })
    await flush()
    const before = Array.from(el.querySelectorAll<HTMLElement>('.inspect-msg'))
      .find(row => 100 + Number.parseFloat(row.style.transform.slice(11)) + rowHeight > body.scrollTop)!
    const key = before.dataset.messageId
    const position = Number.parseFloat(before.style.transform.slice(11)) - body.scrollTop
    el.querySelector<HTMLButtonElement>('.load-earlier')!.click()
    await flush()
    const after = el.querySelector<HTMLElement>(`[data-message-id="${key}"]`)!
    expect(after).toBe(before)
    expect(Number.parseFloat(after.style.transform.slice(11)) - body.scrollTop).toBeCloseTo(position, 4)
    expect(el.querySelectorAll('.inspect-msg').length).toBeLessThan(25)
  })

  it('preserves the leased reading anchor when width invalidates offscreen sizes', async () => {
    const { el, body } = await mountDrawer()
    body.scrollTo({ top: 3_240 })
    await flush()
    // Deliver the initial RO measurements for the newly mounted reading rows,
    // as a browser would before the subsequent width-change interaction.
    for (let pass = 0; pass < 3; pass++) {
      el.querySelectorAll('.inspect-msg').forEach(row => resize(row))
      await flush()
    }
    const before = Array.from(el.querySelectorAll<HTMLElement>('.inspect-msg'))
      .find(row => 100 + Number.parseFloat(row.style.transform.slice(11)) + rowHeight > body.scrollTop)!
    const key = before.dataset.messageId
    const position = Number.parseFloat(before.style.transform.slice(11)) - body.scrollTop
    viewportWidth = 340
    rowHeight = 156
    resize(body)
    await flush()
    await vi.advanceTimersByTimeAsync(32)
    await flush()
    const after = el.querySelector<HTMLElement>(`[data-message-id="${key}"]`)!
    expect(after).not.toBeNull()
    expect(Number.parseFloat(after.style.transform.slice(11)) - body.scrollTop).toBeCloseTo(position, 4)
  })

  it('anchors the first message rather than the summary when history loads at the top', async () => {
    const { el, body } = await mountDrawer()
    body.scrollTo({ top: 0 })
    await flush()
    const before = el.querySelector<HTMLElement>('[data-message-id="message-100"]')!
    const position = Number.parseFloat(before.style.transform.slice(11)) - body.scrollTop
    el.querySelector<HTMLButtonElement>('.load-earlier')!.click()
    await flush()
    const after = el.querySelector<HTMLElement>('[data-message-id="message-100"]')!
    expect(after).not.toBeNull()
    expect(Number.parseFloat(after.style.transform.slice(11)) - body.scrollTop).toBeCloseTo(position, 4)
  })

  it('does not pin to the end when the last measured row grows while reading', async () => {
    const { el, body } = await mountDrawer()
    const previousOffset = body.scrollTop
    rowHeight = 180
    resize(el.querySelector<HTMLElement>('.inspect-msg:last-child')!)
    await flush()
    expect(body.scrollTop).toBe(previousOffset)
  })

  it('clears previous-session measurements when switching an open drawer', async () => {
    const { props } = await mountDrawer()
    const measure = vi.spyOn(state.virtualizer.value, 'measure')
    props.item = { ...props.item, key: 'another-synthetic-session' }
    await flush()
    expect(measure).toHaveBeenCalled()
    expect(state.current.load).toHaveBeenLastCalledWith('another-synthetic-session')
  })

  it.each(['wheel', 'touchstart', 'pointerdown', 'keydown'])('retires an unfinished end seek on %s before a height change', async eventName => {
    // Isolate viewport reconciliation from legitimate first-measure deltas.
    rowHeight = 120
    const { body } = await mountDrawer()
    state.virtualizer.value.scrollToEnd()
    await flush()
    body.dispatchEvent(eventName === 'keydown'
      ? new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true })
      : new Event(eventName, { bubbles: true }))
    body.scrollTo({ top: 2_400 })
    await flush()
    viewportHeight = 300
    resize(body)
    await vi.advanceTimersByTimeAsync(100)
    await flush()
    expect(body.scrollTop).toBe(2_400)
  })

  it('invalidates layout work and releases transcript state when the drawer closes', async () => {
    const { props, body } = await mountDrawer()
    viewportWidth = 340
    resize(body)
    props.open = false
    await flush()
    expect(state.current.reset).toHaveBeenCalledOnce()
  })

  it('cancels an in-flight width lease when directly unmounted while still open', async () => {
    const remeasure = vi.spyOn(virtualizerLayout, 'remeasureVirtualizer')
    const { el, props, body } = await mountDrawer()
    const scroll = vi.spyOn(body, 'scrollTo')
    const removeListener = vi.spyOn(body, 'removeEventListener')
    viewportWidth = 340
    resize(body)
    await flush()
    expect(remeasure).toHaveBeenCalledOnce()
    const options = remeasure.mock.calls[0]![1]
    const transaction = remeasure.mock.results[0]!.value as Promise<boolean>
    expect(options.isCurrent()).toBe(true)
    const scrollCalls = scroll.mock.calls.length

    apps.pop()!.unmount()
    expect(props.open).toBe(true)
    expect(options.isCurrent()).toBe(false)
    await vi.advanceTimersByTimeAsync(64)
    await flush()
    expect(await transaction).toBe(false)
    expect(scroll).toHaveBeenCalledTimes(scrollCalls)
    expect(el.querySelectorAll('.inspect-msg')).toHaveLength(0)
    for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
      expect(removeListener).toHaveBeenCalledWith(event, expect.any(Function))
    }
  })
})
