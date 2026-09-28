// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref, type App } from 'vue'

import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import type { ChatMessageListVirtualizer } from '@/types/chatVirtualizer'
import { consumeProgrammaticScroll } from '@/utils/chat/scrollMutation'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []
let resizeObservers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = []

function message(index: number): ChatRenderedMessage {
  return {
    id: `message-${index}`,
    messageId: `message-${index}`,
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: `Prompt ${index}`,
    timeStr: '',
    showHeader: false,
  }
}

function stubResizeObserver() {
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    constructor(callback: ResizeObserverCallback) {
      resizeObservers.push({ callback, targets: this.targets })
    }
    observe(target: Element) { this.targets.add(target) }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
}

function resize(target: Element) {
  const bounds = target.getBoundingClientRect()
  const entry = { target, borderBoxSize: [{
    blockSize: bounds.height ?? (target as HTMLElement).offsetHeight,
    inlineSize: bounds.width ?? (target as HTMLElement).offsetWidth,
  }] } as unknown as ResizeObserverEntry
  for (const observer of resizeObservers) {
    if (observer.targets.has(target)) observer.callback([entry], {} as ResizeObserver)
  }
}

async function mountList(options: {
  shareMode?: boolean
  forceMountMessageKeys?: ReadonlySet<string>
  followLiveEdge?: boolean
  messages?: ChatRenderedMessage[]
  sessionKey?: string
  scrollEpoch?: number
  trailing?: boolean
  bottomPadding?: number
  withoutScrollContainer?: boolean
  fitsViewport?: boolean
} = {}) {
  const container = document.createElement('div')
  container.dataset.testScroll = 'true'
  const host = document.createElement('div')
  container.appendChild(host)
  document.body.appendChild(container)
  Object.defineProperties(container, {
    offsetHeight: { configurable: true, value: 600 },
    offsetWidth: { configurable: true, value: 800 },
    clientHeight: { configurable: true, value: 600 },
    scrollHeight: { configurable: true, value: options.fitsViewport ? 600 : 18_800 },
    scrollTop: { configurable: true, value: 0, writable: true },
  })
  container.getBoundingClientRect = () => ({ top: 0 } as DOMRect)
  if (options.fitsViewport) {
    vi.spyOn(container, 'scrollTo').mockImplementation(() => { container.scrollTop = 0 })
  }

  const componentProps = reactive({
    messages: options.messages ?? Array.from({ length: 200 }, (_, index) => message(index)),
    scrollContainer: options.withoutScrollContainer ? undefined : container,
    shareMode: options.shareMode ?? false,
    forceMountMessageKeys: options.forceMountMessageKeys,
    followLiveEdge: options.followLiveEdge,
    sessionKey: options.sessionKey,
    scrollEpoch: options.scrollEpoch ?? 0,
    bottomPadding: options.bottomPadding ?? 0,
    selectedMessageIds: new Set<string>(),
    stripTimePrefix: (value: string) => value,
    renderMarkdown: (value: string) => value,
    fmtTok: (value: number) => String(value),
    subagentSummary: (value: string) => value,
    subagentBody: (value: string) => value,
    toolCallGroups: () => [],
    isToolGroupOpen: () => false,
    isToolItemOpen: () => false,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
    downloadAttachment: async () => true,
  })
  const listRef = ref<ChatMessageListVirtualizer | null>(null)
  const app = createApp({
    setup: () => () => h(ChatMessageList, { ...componentProps, ref: listRef },
      options.trailing ? { trailing: () => h('div', 'Streaming answer') } : undefined),
  })
  app.use(i18n)
  app.mount(host)
  const api = listRef.value as ChatMessageListVirtualizer
  apps.push(app)
  await nextTick()
  await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))
  await nextTick()
  return { api, container, host, props: componentProps }
}

beforeEach(() => {
  resizeObservers = []
  stubResizeObserver()
  window.localStorage.clear()
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const height = this.dataset.testid === 'chat-message-row' ? 94 : 0
    const scrollTop = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
    const top = this.classList.contains('chat-message-list') ? -scrollTop
      : this.dataset.chatMessageIndex ? Number(this.dataset.chatMessageIndex) * 94 - scrollTop : 0
    return { top, bottom: top + height, height, width: 800, left: 0, right: 800, x: 0, y: top, toJSON: () => ({}) }
  })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.getBoundingClientRect().height
  })
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  window.localStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ChatMessageList long-history virtualization', () => {
  it('settles clamped short-transcript measurements without idle frame polling', async () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })
    try {
      const frames = vi.spyOn(window, 'requestAnimationFrame')
      const mounted = mountList({
        messages: [message(0)], followLiveEdge: true, fitsViewport: true,
      })
      await vi.advanceTimersByTimeAsync(32)
      const { host, api } = await mounted
      const row = host.querySelector('[data-chat-message-index="0"]') as HTMLElement
      row.getBoundingClientRect = () => ({ height: 130, width: 800, top: 0 } as DOMRect)
      resize(row)
      await nextTick()
      await nextTick()
      const before = frames.mock.calls.length
      await vi.advanceTimersByTimeAsync(1000)
      expect(api.getDistanceFromEnd()).toBe(0)
      expect(frames.mock.calls.length - before).toBeLessThanOrEqual(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports a viewport resize before the native observer consumes it', async () => {
    const { api, container } = await mountList({ followLiveEdge: true })
    expect(api.hasPendingLayout()).toBe(false)
    Object.defineProperty(container, 'offsetHeight', { configurable: true, value: 400 })
    Object.defineProperty(container, 'clientHeight', { configurable: true, value: 400 })
    expect(api.hasPendingLayout()).toBe(true)
    resize(container)
    await nextTick()
    await nextTick()
    expect(api.hasPendingLayout()).toBe(false)
    expect(api.getDistanceFromEnd()).toBe(0)
  })

  it.each([2, 200])('exposes live bottom distance for %i rows before a native scroll notification', async count => {
    const { api, container, props } = await mountList({
      messages: Array.from({ length: count }, (_, index) => message(index)),
    })
    expect(api.getDistanceFromEnd()).toBe(18_200)
    container.scrollTop = 18_199.5
    expect(api.getDistanceFromEnd()).toBe(0.5)
    Object.defineProperty(container, 'clientHeight', { configurable: true, value: 500 })
    expect(api.getDistanceFromEnd()).toBe(100.5)
    container.dispatchEvent(new Event('scroll'))
    expect(api.getDistanceFromEnd()).toBe(100.5)
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 18_000 })
    expect(api.getDistanceFromEnd()).toBe(0)
    props.scrollContainer = undefined
    await nextTick()
    expect(api.getDistanceFromEnd()).toBe(Infinity)
  })

  it('mounts a bounded two-viewport window for a 200-message transcript', async () => {
    const { api, host } = await mountList()

    expect(api.isVirtualized()).toBe(true)
    expect(host.querySelector('.chat-message-list')?.getAttribute('data-virtualized')).toBe('true')
    expect(host.querySelectorAll('[data-testid="chat-message-row"]').length).toBeLessThanOrEqual(30)
    expect(host.querySelectorAll('[data-testid="chat-message-row"]').length).toBeGreaterThan(0)
    expect(host.querySelector('[data-chat-message-index="199"]')).toBeNull()
  })

  it('pins a logical destination without mounting the messages between it and the viewport', async () => {
    const { api, host } = await mountList()

    const target = await api.ensureMessageVisible(150)
    expect(target?.id).toBe('chat-turn-150')
    expect(host.querySelector('[data-chat-message-index="150"]')).toBeTruthy()
    expect(host.querySelectorAll('[data-testid="chat-message-row"]').length).toBeLessThanOrEqual(30)

    api.releaseEnsuredMessage(150)
    await nextTick()
    expect(host.querySelector('[data-chat-message-index="150"]')).toBeNull()
  })

  it('keeps an externally owned search match mounted', async () => {
    const { host } = await mountList({
      forceMountMessageKeys: new Set(['message-175']),
    })

    expect(host.querySelector('[data-chat-message-index="175"]')).toBeTruthy()
  })

  it('resolves the same stable destination when history prepends during its mount', async () => {
    const { api, props } = await mountList()
    const pending = api.ensureMessageVisible(150)
    props.messages = [message(-1), ...props.messages]
    const target = await pending
    expect(target?.dataset.chatTurnKey).toBe('message-150')
    expect(target?.id).toBe('chat-turn-151')
  })

  it('does not hand an old-session destination to a replacement session', async () => {
    const { api, props } = await mountList({ sessionKey: 'session-a' })
    const pending = api.ensureMessageVisible(150)
    props.sessionKey = 'session-b'
    expect(await pending).toBeNull()
  })

  it('keeps 200 history rows plus 40 settled terminal rows within the DOM ceiling', async () => {
    const messages = Array.from({ length: 240 }, (_, index) => ({
      ...message(index),
      ...(index >= 200
        ? index % 2 === 0
          ? { stopNotice: true }
          : { terminalFailure: true }
        : {}),
    }))
    const { host } = await mountList({ messages })

    expect(host.querySelectorAll('[data-testid="chat-message-row"]').length).toBeLessThanOrEqual(30)
    expect(host.querySelector('[data-chat-message-index="239"]')).toBeNull()
  })

  it('re-pins the live edge after variable row heights replace estimates', async () => {
    const { container, host } = await mountList({ followLiveEdge: true })
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 12_000 })
    const row = host.querySelector<HTMLElement>('[data-testid="chat-message-row"]')
    expect(row).toBeTruthy()
    row!.getBoundingClientRect = () => ({ height: 40 } as DOMRect)
    resize(row!)
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(12_000 - container.clientHeight)
  })

  it('keeps the live edge pinned when the chat viewport changes size', async () => {
    const { container } = await mountList({ followLiveEdge: true })
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 12_000 })
    container.scrollTop = 8_400
    resize(container)
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(12_000 - container.clientHeight)
  })

  it('leaves a historical reader in place when the chat viewport changes size', async () => {
    const { container } = await mountList({ followLiveEdge: false })
    container.scrollTop = 8_400
    resize(container)
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(8_400)
  })

  it('retires an unfinished live-edge seek when a source-less reader scroll pauses follow', async () => {
    const { api, container, props } = await mountList({ followLiveEdge: true })
    api.scrollToEnd()
    container.scrollTop = 16_000
    container.dispatchEvent(new Event('scroll'))
    props.followLiveEdge = false
    await nextTick()
    Object.defineProperty(container, 'offsetHeight', { configurable: true, value: 800 })
    Object.defineProperty(container, 'clientHeight', { configurable: true, value: 800 })
    resize(container)
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(16_000)
  })

  it('does not cancel a new leased navigation when follow pauses in the same Vue tick', async () => {
    const { api, container, host, props } = await mountList({ followLiveEdge: true })
    api.scrollToEnd()
    await api.ensureMessageVisible(5)
    props.followLiveEdge = false
    api.scrollToMessage(5, { align: 'start', behavior: 'auto' })
    await nextTick()
    let width = 800
    const root = host.querySelector<HTMLElement>('.chat-message-list')!
    root.getBoundingClientRect = () => ({ width, top: -container.scrollTop } as DOMRect)
    const preceding = host.querySelector<HTMLElement>('[data-chat-message-index="4"]')!
    preceding.getBoundingClientRect = () => ({ height: width === 640 ? 140 : 94 } as DOMRect)
    width = 640
    // Navigation still owns this reflow. Reissuing the leased destination
    // must keep its 16px inset instead of restoring the previous end seek.
    resize(root)
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(4 * 94 + 140 - 16)
    expect(host.querySelector('[data-chat-message-index="5"]')).toBeTruthy()
  })

  it('gives a terminal text handoff position ownership and resumes geometry compensation on release', async () => {
    let completed = false
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const height = this.classList.contains('chat-message-list__trailing') ? (completed ? 0 : 2_000)
        : this.dataset.chatMessageKey === 'message-200' ? 1_600
        : this.dataset.testid === 'chat-message-row' ? 94 : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      return { top: this.classList.contains('chat-message-list') ? -offset : 0, height, width: 800 } as DOMRect
    })
    const { api, container, host, props } = await mountList({ trailing: true, followLiveEdge: false })
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 22_000 })
    container.scrollTop = 19_000
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    const release = api.beginScrollHandoff()
    completed = true
    props.messages = [...props.messages, message(200)]
    await nextTick()
    resize(host.querySelector('.chat-message-list__trailing')!)
    await nextTick()
    expect(container.scrollTop).toBe(19_000)
    // A text restore writes directly. Releasing must publish it to the public
    // offset observer even before the browser delivers the resulting event.
    container.scrollTop = 18_900
    release()
    release()
    await nextTick()
    const above = host.querySelector<HTMLElement>('[data-chat-message-index="199"]')!
    above.getBoundingClientRect = () => ({ height: 114 } as DOMRect)
    resize(above)
    await nextTick()
    expect(container.scrollTop).toBe(18_920)
    props.messages = [message(-1), ...props.messages]
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(19_014)
  })

  it('never replays a handoff position after native reader movement or a new scroll epoch', async () => {
    const { api, container, props } = await mountList({ followLiveEdge: false, scrollEpoch: 1 })
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    const release = api.beginScrollHandoff()
    container.scrollTop = 1_128
    container.dispatchEvent(new Event('scroll'))
    release()
    expect(container.scrollTop).toBe(1_128)
    const staleRelease = api.beginScrollHandoff()
    props.scrollEpoch = 2
    await nextTick()
    container.scrollTop = 282
    staleRelease()
    expect(container.scrollTop).toBe(282)
  })

  it('does not release a newer semantic handoff from an older completion callback', async () => {
    const { api, container, host } = await mountList({ followLiveEdge: false })
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    const previous = api.beginScrollHandoff()
    const current = api.beginScrollHandoff()
    previous()
    const above = host.querySelector<HTMLElement>('[data-chat-message-index="5"]')!
    above.getBoundingClientRect = () => ({ height: 194 } as DOMRect)
    resize(above)
    await nextTick()
    expect(container.scrollTop).toBe(940)
    current()
  })

  it('lets TanStack compensate an above-viewport resize exactly once', async () => {
    const { container, host } = await mountList()
    container.scrollTop = 94
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    const row = host.querySelector<HTMLElement>('[data-chat-message-index="0"]')!
    row.getBoundingClientRect = () => ({ height: 214 } as DOMRect)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(214)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(214)
  })

  it('does not end-pin a growing last row after the reader releases follow', async () => {
    const { container, host } = await mountList({ followLiveEdge: false })
    container.scrollTop = 18_200
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    const row = host.querySelector<HTMLElement>('[data-chat-message-index="199"]')!
    expect(row).toBeTruthy()
    row.getBoundingClientRect = () => ({ height: 194 } as DOMRect)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(18_200)
  })

  it('keeps an upward destination stable when an earlier code toolbar finishes mounting', async () => {
    const { api, container, host } = await mountList({ followLiveEdge: false })
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    await api.ensureMessageVisible(5)
    api.scrollToMessage(5, { align: 'start', behavior: 'auto' })
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    const before = container.scrollTop
    const row = host.querySelector<HTMLElement>('[data-chat-message-index="3"]')!
    row.getBoundingClientRect = () => ({ height: 122 } as DOMRect)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(before + 28)
  })

  it('does not compensate growth below the reading line inside a long row', async () => {
    const { container, host } = await mountList({ followLiveEdge: false })
    const row = host.querySelector<HTMLElement>('[data-chat-message-index="1"]')!
    row.getBoundingClientRect = () => ({ height: 94.5 } as DOMRect)
    resize(row)
    await nextTick()
    container.scrollTop = 120
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    row.getBoundingClientRect = () => ({ height: 594.5 } as DOMRect)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(120)
  })

  it('does not drag a visible row whose initial size exactly matched its estimate', async () => {
    const { container, host } = await mountList({ followLiveEdge: false })
    const row = host.querySelector<HTMLElement>('[data-chat-message-index="1"]')!
    expect(row.getBoundingClientRect().height).toBe(94)
    container.scrollTop = 120
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    row.getBoundingClientRect = () => ({ height: 594 } as DOMRect)
    resize(row)
    await nextTick()
    expect(container.scrollTop).toBe(120)
  })

  it('keeps an explicitly leased destination through a width remeasurement', async () => {
    const { api, container } = await mountList({ followLiveEdge: false })
    await api.ensureMessageVisible(150)
    api.scrollToMessage(150, { align: 'start', behavior: 'auto' })
    const target = container.scrollTop
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    api.remeasure()
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(target)
  })

  it('preserves the keyed reading position when older history is prepended', async () => {
    const { container, props } = await mountList({
      messages: Array.from({ length: 100 }, (_, index) => message(index + 100)),
    })
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    props.messages = [
      ...Array.from({ length: 50 }, (_, index) => message(index + 50)),
      ...props.messages,
    ]
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(940 + 50 * 94)
  })

  it('keeps first-measure prepend correction when a history header crosses the fold', async () => {
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const row = this.dataset.testid === 'chat-message-row'
      const height = row ? (this.dataset.chatMessageKey === 'message-99' ? 140 : 94) : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      const top = this.classList.contains('chat-message-list') ? 48 - offset : 0
      return { top, bottom: top + height, height, width: 800 } as DOMRect
    })
    const { container, props } = await mountList({
      messages: Array.from({ length: 100 }, (_, index) => message(index + 100)),
      followLiveEdge: false,
    })
    props.messages = [message(99), ...props.messages]
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(140)
  })

  it('waits for the larger Vue DOM before applying an unreachable prepend anchor', async () => {
    const { container, host, props } = await mountList({
      messages: Array.from({ length: 50 }, (_, index) => message(index + 50)),
      followLiveEdge: false,
    })
    let top = 0
    Object.defineProperties(container, {
      scrollHeight: { configurable: true, get: () => (
        host.querySelector('.chat-message-list')?.getAttribute('data-virtualized') === 'true'
          ? 9_400 : 4_700
      ) },
      scrollTop: { configurable: true, get: () => top, set: (value: number) => {
        top = Math.max(0, Math.min(value, container.scrollHeight - container.clientHeight))
      } },
    })
    props.messages = [...Array.from({ length: 50 }, (_, index) => message(index)), ...props.messages]
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(4_700)
  })

  it('preserves a reading row when the external history header disappears', async () => {
    let margin = 48
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const height = this.dataset.testid === 'chat-message-row' ? 94 : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      const top = this.classList.contains('chat-message-list') ? margin - offset : 0
      return { top, bottom: top + height, height, width: 800 } as DOMRect
    })
    const { container, host } = await mountList({ followLiveEdge: false })
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    margin = 4
    resize(host.querySelector('.chat-message-list')!)
    await nextTick()
    await nextTick()
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(896)
  })

  it('preserves the same row when the final prepend also removes its history header', async () => {
    let margin = 48
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const height = this.dataset.testid === 'chat-message-row' ? 94 : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      const top = this.classList.contains('chat-message-list') ? margin - offset : 0
      return { top, bottom: top + height, height, width: 800 } as DOMRect
    })
    const { container, props } = await mountList({
      messages: Array.from({ length: 150 }, (_, index) => message(index + 50)),
      followLiveEdge: false,
    })
    props.messages = [...Array.from({ length: 50 }, (_, index) => message(index)), ...props.messages]
    margin = 4
    await nextTick()
    await nextTick()
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(50 * 94 - 44)
  })

  it('releases forced destinations and cancels navigation on a new scroll epoch', async () => {
    const { api, host, props } = await mountList({ scrollEpoch: 1 })
    await api.ensureMessageVisible(150)
    expect(host.querySelector('[data-chat-message-index="150"]')).toBeTruthy()
    props.scrollEpoch = 2
    await nextTick()
    await nextTick()
    expect(host.querySelector('[data-chat-message-index="150"]')).toBeNull()
  })

  it('invalidates a queued pin when switching to a reading session', async () => {
    const { container, host, props } = await mountList({
      followLiveEdge: true, sessionKey: 'session-a', scrollEpoch: 1,
    })
    const row = host.querySelector<HTMLElement>('[data-testid="chat-message-row"]')!
    row.getBoundingClientRect = () => ({ height: 40 } as DOMRect)
    resize(row)
    props.sessionKey = 'session-b'
    props.messages = Array.from({ length: 200 }, (_, index) => message(index + 200))
    props.followLiveEdge = false
    props.scrollEpoch += 1
    container.scrollTop = 100
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(100)
  })

  it('renders the complete canonical history for share mode and the rollback flag', async () => {
    const shared = await mountList({ shareMode: true })
    expect(shared.host.querySelectorAll('[data-testid="chat-message-row"]')).toHaveLength(200)
    expect(shared.api.isVirtualized()).toBe(false)

    window.localStorage.setItem('opensquilla.chat.virtualizeHistory', '0')
    const rollback = await mountList()
    expect(rollback.host.querySelectorAll('[data-testid="chat-message-row"]')).toHaveLength(200)
    expect(rollback.api.isVirtualized()).toBe(false)
  })

  it('measures the trailing stream in the same virtual range and reserves bottom space once', async () => {
    const { host } = await mountList({ trailing: true, bottomPadding: 24 })
    const tail = host.querySelector<HTMLElement>('[data-testid="chat-message-trailing"]')!
    expect(tail).toBeTruthy()
    expect(tail.dataset.index).toBe('200')
    expect(host.querySelectorAll('[data-chat-message-index]').length).toBeLessThanOrEqual(30)
    const bottom = host.querySelector<HTMLElement>('[data-testid="chat-history-bottom-spacer"]')!
    expect(bottom.style.height).toBe('24px')
  })

  it('renders full history for legacy embedders without a scroll container', async () => {
    const { host, api } = await mountList({ withoutScrollContainer: true })
    expect(host.querySelectorAll('[data-testid="chat-message-row"]')).toHaveLength(200)
    expect(api.isVirtualized()).toBe(false)
  })

  it('invalidates sizes while retaining the same reading row and fractional inset', async () => {
    const { api, container } = await mountList()
    container.scrollTop = 940.25
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    api.remeasure()
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(940.25)
  })

  it('keeps the first complete row when a mostly clipped short row grows during width reflow', async () => {
    let width = 800
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const row = this.dataset.testid === 'chat-message-row'
      const index = Number(this.dataset.chatMessageIndex)
      const extra = width === 600 ? 42 : 0
      const height = row ? 94 + (index === 10 ? extra : 0) : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      const top = this.classList.contains('chat-message-list') ? -offset
        : row ? index * 94 + (index > 10 ? extra : 0) - offset : 0
      return { top, bottom: top + height, height, width } as DOMRect
    })
    const { container, host } = await mountList({
      messages: Array.from({ length: 48 }, (_, index) => message(index)), followLiveEdge: false,
    })
    container.scrollTop = 1_010 // Row 10 is mostly clipped; row 11 starts 24px below the fold.
    container.dispatchEvent(new Event('scroll'))
    await nextTick() // Reflow immediately, without waiting for scroll-idle/text anchoring.
    width = 600
    resize(host.querySelector('.chat-message-list')!)
    resize(host.querySelector('[data-chat-message-index="10"]')!)
    await nextTick()
    await nextTick()
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(1_052)
    width = 800
    resize(host.querySelector('.chat-message-list')!)
    resize(host.querySelector('[data-chat-message-index="10"]')!)
    await nextTick()
    await nextTick()
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(1_010)
  })

  it('keeps an oversized current row even when a complete following row is visible', async () => {
    let width = 800
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const row = this.dataset.testid === 'chat-message-row'
      const index = Number(this.dataset.chatMessageIndex)
      const height = row ? (index === 10 ? (width === 800 ? 1_200 : 1_600) : 94) : 0
      const offset = this.closest<HTMLElement>('[data-test-scroll]')?.scrollTop ?? 0
      const top = this.classList.contains('chat-message-list') ? -offset : 0
      return { top, bottom: top + height, height, width } as DOMRect
    })
    const { container, host } = await mountList({
      messages: Array.from({ length: 48 }, (_, index) => message(index)), followLiveEdge: false,
    })
    container.scrollTop = 1_740 // 800px into a 1200px message: keep its reading point.
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    width = 600
    resize(host.querySelector('.chat-message-list')!)
    await nextTick()
    await nextTick()
    await new Promise(resolve => window.requestAnimationFrame(resolve))
    await nextTick()
    expect(container.scrollTop).toBe(1_740)
  })

  it('does not finish a width correction after fresh reader navigation cancels it', async () => {
    const { api, container } = await mountList()
    container.scrollTop = 940
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    api.remeasure()
    api.cancelScroll()
    container.scrollTop = 1_040
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(1_040)
  })

  it('never labels an already-applied wheel delta as a programmatic cancellation', async () => {
    const { api, container, props } = await mountList({ followLiveEdge: true })
    consumeProgrammaticScroll(container)
    // Chromium may commit a compositor wheel before dispatching its JS event.
    container.scrollTop = 0
    const scrollTo = vi.spyOn(container, 'scrollTo')
    api.cancelScroll()
    expect(scrollTo).not.toHaveBeenCalled()
    expect(consumeProgrammaticScroll(container)).toBeNull()
    // The owning view can now identify this unmarked scroll as reader input.
    props.followLiveEdge = false
    container.dispatchEvent(new Event('scroll'))
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(0)
    api.scrollToEnd()
    expect(container.scrollTop).toBe(container.scrollHeight - container.clientHeight)
  })

  it('does not pause live following when a nested scroller only cancels navigation', async () => {
    const { api, container } = await mountList({ followLiveEdge: true })
    api.cancelScroll()
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 20_000 })
    resize(container)
    await nextTick()
    await nextTick()
    expect(container.scrollTop).toBe(20_000 - container.clientHeight)
  })
})
