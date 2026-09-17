// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { chatMessageKey } from '@/utils/chat/messageIdentity'
import ConversationMinimap from './ConversationMinimap.vue'

interface ThreadFixture {
  container: HTMLElement
  offsets: number[]
  scrollTo: ReturnType<typeof vi.fn>
}

interface MountOptions {
  sessionKey?: string
  historyHasMore?: boolean
  onNavigate?: ReturnType<typeof vi.fn>
  onNavigateEnd?: ReturnType<typeof vi.fn>
  ensureMessageVisible?: (sourceIndex: number) => Promise<HTMLElement | null>
  releaseEnsuredMessage?: (sourceIndex?: number) => void
  messageOffset?: (sourceIndex: number) => number | null
}

interface ThreadDimensions {
  clientWidth?: number
  clientHeight?: number
  scrollHeight?: number
}

interface ResizeObserverFixture {
  callback: ResizeObserverCallback
  targets: Set<Element>
}

const mountedApps: App<Element>[] = []

function stubResizeObservers(): ResizeObserverFixture[] {
  const observers: ResizeObserverFixture[] = []
  vi.stubGlobal('ResizeObserver', class {
    callback: ResizeObserverCallback
    targets = new Set<Element>()

    constructor(callback: ResizeObserverCallback) {
      this.callback = callback
      observers.push(this)
    }

    observe(target: Element) { this.targets.add(target) }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
  return observers
}

function message(role: 'user' | 'assistant', index: number): ChatRenderedMessage {
  return {
    id: `${role}-${index}`,
    messageId: `${role}-${index}`,
    role,
    displayRole: role,
    roleLabel: role,
    text: role === 'user' ? `Remember the detail from prompt ${index}` : `Answer ${index}`,
    timeStr: `10:${String(index).padStart(2, '0')}`,
    showHeader: false,
  }
}

function messages(turnCount = 8): ChatRenderedMessage[] {
  return Array.from({ length: turnCount }, (_, index) => [message('user', index), message('assistant', index)]).flat()
}

function rect(top: number, height: number): DOMRect {
  return {
    x: 0,
    y: top,
    top,
    bottom: top + height,
    left: 0,
    right: 800,
    width: 800,
    height,
    toJSON: () => ({}),
  } as DOMRect
}

function mediaQueryList(media: string, matches: boolean): MediaQueryList {
  return {
    matches,
    media,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: () => true,
  } as unknown as MediaQueryList
}

function makeThread(
  rendered: ChatRenderedMessage[],
  clientWidth = 1200,
  dimensions: ThreadDimensions = {},
): ThreadFixture {
  const container = document.createElement('div')
  const clientHeight = dimensions.clientHeight ?? 600
  const offsets = rendered
    .map((entry, sourceIndex) => ({ entry, sourceIndex }))
    .filter(({ entry }) => entry.displayRole === 'user')
    .map((_, index) => index * 400)

  Object.defineProperties(container, {
    clientWidth: { configurable: true, value: clientWidth },
    clientHeight: { configurable: true, value: clientHeight },
    scrollHeight: {
      configurable: true,
      value: dimensions.scrollHeight ?? Math.max(3000, offsets[offsets.length - 1] + 800),
    },
    scrollTop: { configurable: true, value: 0, writable: true },
  })
  container.getBoundingClientRect = () => rect(0, clientHeight)

  rendered.forEach((entry, sourceIndex) => {
    if (entry.displayRole !== 'user') return
    const turnIndex = rendered.slice(0, sourceIndex).filter(item => item.displayRole === 'user').length
    const anchor = document.createElement('div')
    anchor.id = `chat-turn-${sourceIndex}`
    anchor.dataset.chatTurnKey = chatMessageKey(entry, sourceIndex)
    anchor.tabIndex = -1
    anchor.getBoundingClientRect = () => rect(offsets[turnIndex] - container.scrollTop, 80)
    container.appendChild(anchor)
  })

  const scrollTo = vi.fn((options: ScrollToOptions) => {
    container.scrollTop = Number(options.top || 0)
    container.dispatchEvent(new Event('scroll'))
  })
  container.scrollTo = scrollTo as unknown as typeof container.scrollTo
  document.body.appendChild(container)
  return { container, offsets, scrollTo }
}

async function mountMinimap(
  turnCount = 8,
  options: MountOptions = {},
  dimensions: ThreadDimensions = {},
) {
  const rendered = messages(turnCount)
  const thread = makeThread(rendered, dimensions.clientWidth ?? 1200, dimensions)
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(ConversationMinimap, {
    messages: rendered,
    scrollContainer: thread.container,
    stripTimePrefix: (value: string) => value,
    sessionKey: options.sessionKey,
    historyHasMore: options.historyHasMore,
    onNavigate: options.onNavigate,
    onNavigateEnd: options.onNavigateEnd,
    ensureMessageVisible: options.ensureMessageVisible,
    releaseEnsuredMessage: options.releaseEnsuredMessage,
    messageOffset: options.messageOffset,
  })
  app.use(i18n)
  const instance = app.mount(host) as unknown as { cancelNavigation: () => void }
  mountedApps.push(app)
  await nextTick()
  await vi.waitFor(() => expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeTruthy())
  return { host, instance, thread }
}

function markers(host: HTMLElement): HTMLButtonElement[] {
  return Array.from(host.querySelectorAll<HTMLButtonElement>('[data-testid="conversation-minimap-marker"]'))
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ConversationMinimap', () => {
  it('renders one accessible marker per user prompt only for long histories', async () => {
    const { host } = await mountMinimap()
    const nav = host.querySelector<HTMLElement>('nav')
    const rows = markers(host)

    expect(nav?.getAttribute('aria-label')).toBe('Conversation history, 8 prompts')
    expect(rows).toHaveLength(8)
    expect(rows[0].getAttribute('aria-label')).toContain('Remember the detail from prompt 0')
    expect(rows.filter(row => row.tabIndex === 0)).toHaveLength(1)
    expect(rows.filter(row => row.getAttribute('aria-current') === 'location')).toHaveLength(1)
  })

  it('shows a compact prompt preview on hover', async () => {
    const { host } = await mountMinimap()
    const row = markers(host)[2]

    row.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()

    const tooltip = host.querySelector<HTMLElement>('[role="tooltip"]')
    expect(tooltip?.textContent).toContain('Prompt 3 of 8')
    expect(tooltip?.textContent).toContain('Remember the detail from prompt 2')
    expect(row.getAttribute('aria-describedby')).toBe(tooltip?.id)
    expect(tooltip?.parentElement?.classList.contains('conversation-minimap__preview-positioner')).toBe(true)
    expect(tooltip?.parentElement?.style.getPropertyValue('--conversation-minimap-preview-y')).toBeTruthy()

    row.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    await nextTick()
    expect(row.hasAttribute('aria-describedby')).toBe(false)
    await vi.waitFor(() => expect(host.querySelector('[role="tooltip"]')).toBeNull())
  })

  it('keeps idle markers short while tracking the current prompt with thickness and opacity', async () => {
    const { host, thread } = await mountMinimap()
    thread.container.scrollTop = 1000
    thread.container.dispatchEvent(new Event('scroll'))
    await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))

    const rows = markers(host)
    const styleValue = (index: number, property: string) => (
      Number(rows[index].style.getPropertyValue(property))
    )
    const widthScale = (index: number) => styleValue(
      index,
      '--conversation-minimap-line-scale-x',
    )
    const heightScale = (index: number) => styleValue(
      index,
      '--conversation-minimap-line-scale-y',
    )
    const opacity = (index: number) => styleValue(
      index,
      '--conversation-minimap-line-opacity',
    )

    expect(rows.map((_, index) => widthScale(index))).toEqual(Array(8).fill(0.2667))
    expect(rows[2].getAttribute('aria-current')).toBe('location')
    expect(rows.filter(row => row.hasAttribute('aria-current'))).toHaveLength(1)
    expect(heightScale(2)).toBe(1)
    expect(opacity(2)).toBe(1)
    expect(heightScale(1)).toBe(0.5)
    expect(opacity(1)).toBe(0.45)
  })

  it('uses a continuous neighboring lens without remounting the preview while scrubbing', async () => {
    const { host } = await mountMinimap()
    const rows = markers(host)
    rows.forEach((row, index) => {
      row.getBoundingClientRect = () => rect(index * 16, 16)
    })
    rows[3].dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()
    const initialTooltip = host.querySelector<HTMLElement>('[role="tooltip"]')

    host.querySelector<HTMLElement>('.conversation-minimap__list')?.dispatchEvent(
      new MouseEvent('pointermove', { bubbles: true, clientY: 64 }),
    )
    await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))
    await nextTick()

    const scale = (index: number) => Number(rows[index].style.getPropertyValue('--conversation-minimap-line-scale-x'))
    expect(scale(3)).toBeCloseTo(scale(4), 3)
    expect(scale(3)).toBeGreaterThan(scale(2))
    expect(scale(4)).toBeGreaterThan(scale(5))
    expect(Number(
      rows[0].style.getPropertyValue('--conversation-minimap-line-scale-y'),
    )).toBe(1)
    expect(Number(
      rows[3].style.getPropertyValue('--conversation-minimap-line-scale-y'),
    )).toBe(0.5)
    expect(host.querySelector('[role="tooltip"]')).toBe(initialTooltip)
  })

  it('collapses the pointer lens after leaving the rail', async () => {
    const { host } = await mountMinimap()
    const rows = markers(host)
    const widthScale = (index: number) => Number(
      rows[index].style.getPropertyValue('--conversation-minimap-line-scale-x'),
    )

    rows[3].dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()
    expect(widthScale(3)).toBe(1)
    expect(widthScale(2)).toBeGreaterThan(0.2667)

    host.querySelector<HTMLElement>('.conversation-minimap__list')?.dispatchEvent(
      new MouseEvent('pointerleave'),
    )
    await nextTick()

    expect(rows.map((_, index) => widthScale(index))).toEqual(Array(8).fill(0.2667))
    await vi.waitFor(() => expect(host.querySelector('[role="tooltip"]')).toBeNull())
  })

  it('jumps to a prompt without forcing the conversation to the live edge', async () => {
    const onNavigate = vi.fn()
    const onNavigateEnd = vi.fn()
    const { host, thread } = await mountMinimap(8, { onNavigate, onNavigateEnd })
    markers(host)[3].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))

    expect(onNavigate).toHaveBeenCalledWith(3)
    expect(onNavigate.mock.invocationCallOrder[0]).toBeLessThan(thread.scrollTo.mock.invocationCallOrder[0])
    expect(thread.scrollTo).toHaveBeenCalledWith({
      top: thread.offsets[3] - 16,
      behavior: 'smooth',
    })
    expect(thread.container.scrollTop).toBeLessThan(thread.container.scrollHeight - thread.container.clientHeight)
    thread.container.dispatchEvent(new Event('scroll'))
    expect(onNavigateEnd).not.toHaveBeenCalled()
    thread.container.dispatchEvent(new Event('scrollend'))
    expect(onNavigateEnd).toHaveBeenCalledOnce()
    expect(thread.container.querySelector('[data-chat-turn-key="user-3"]')?.classList.contains('is-history-target')).toBe(true)
  })

  it('materializes an unmounted logical prompt before minimap navigation', async () => {
    let threadContainer: HTMLElement | null = null
    const releaseEnsuredMessage = vi.fn()
    const ensureMessageVisible = vi.fn(async (sourceIndex: number) => {
      const anchor = document.createElement('div')
      anchor.id = `chat-turn-${sourceIndex}`
      anchor.dataset.chatTurnKey = `user-${sourceIndex / 2}`
      anchor.tabIndex = -1
      anchor.getBoundingClientRect = () => rect((sourceIndex / 2) * 400 - (threadContainer?.scrollTop || 0), 80)
      threadContainer?.appendChild(anchor)
      return anchor
    })
    const mounted = await mountMinimap(8, {
      ensureMessageVisible,
      releaseEnsuredMessage,
      messageOffset: sourceIndex => (sourceIndex / 2) * 400,
    })
    threadContainer = mounted.thread.container
    mounted.thread.container.querySelector('[data-chat-turn-key="user-3"]')?.remove()

    markers(mounted.host)[3].click()
    await vi.waitFor(() => expect(mounted.thread.scrollTo).toHaveBeenCalled())

    expect(ensureMessageVisible).toHaveBeenCalledWith(6)
    expect(mounted.thread.scrollTo).toHaveBeenLastCalledWith({ top: 1_184, behavior: 'smooth' })
    mounted.thread.container.dispatchEvent(new Event('scrollend'))
    expect(releaseEnsuredMessage).toHaveBeenCalledWith(6)
  })

  it('pairs navigation lifecycle when deferred materialization is cancelled', async () => {
    const deferred: { resolve?: (element: HTMLElement | null) => void } = {}
    const ensureMessageVisible = vi.fn(() => new Promise<HTMLElement | null>(resolve => {
      deferred.resolve = resolve
    }))
    const releaseEnsuredMessage = vi.fn()
    const onNavigate = vi.fn()
    const onNavigateEnd = vi.fn()
    const mounted = await mountMinimap(8, {
      ensureMessageVisible,
      onNavigate,
      onNavigateEnd,
      releaseEnsuredMessage,
    })
    mounted.thread.container.querySelector('[data-chat-turn-key="user-3"]')?.remove()

    markers(mounted.host)[3].click()
    await vi.waitFor(() => expect(ensureMessageVisible).toHaveBeenCalledWith(6))
    expect(onNavigate).toHaveBeenCalledOnce()
    expect(onNavigateEnd).not.toHaveBeenCalled()

    mounted.instance.cancelNavigation()
    expect(onNavigateEnd).toHaveBeenCalledOnce()

    const lateAnchor = document.createElement('div')
    deferred.resolve!(lateAnchor)
    await nextTick()
    await vi.waitFor(() => expect(releaseEnsuredMessage).toHaveBeenCalledWith(6))
    expect(mounted.thread.scrollTo).not.toHaveBeenCalled()
    expect(onNavigateEnd).toHaveBeenCalledOnce()
  })

  it('completes immediately when the selected prompt is already in place', async () => {
    const onNavigateEnd = vi.fn()
    const { host, thread } = await mountMinimap(8, { onNavigateEnd })

    markers(host)[0].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))

    expect(thread.scrollTo).not.toHaveBeenCalled()
    expect(onNavigateEnd).toHaveBeenCalledOnce()
    expect(thread.container.querySelector('[data-chat-turn-key="user-0"]')?.classList.contains('is-history-target')).toBe(true)
  })

  it('keeps the selected marker stable during navigation and reconciles after arrival', async () => {
    const { host, thread } = await mountMinimap()
    markers(host)[3].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))

    thread.container.scrollTop = 400
    thread.container.dispatchEvent(new Event('scroll'))
    await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))
    expect(markers(host)[3].getAttribute('aria-current')).toBe('location')

    thread.container.dispatchEvent(new Event('scrollend'))
    await vi.waitFor(() => expect(markers(host)[1].getAttribute('aria-current')).toBe('location'))
    expect(thread.container.querySelector('[data-chat-turn-key="user-3"]')?.classList.contains('is-history-target')).toBe(false)
  })

  it('does not mark a cancelled destination as reached', async () => {
    const { host, thread } = await mountMinimap()
    const firstTarget = thread.container.querySelector('[data-chat-turn-key="user-3"]')!

    markers(host)[3].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    thread.container.scrollTop = 200
    markers(host)[4].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))

    expect(firstTarget.classList.contains('is-history-target')).toBe(false)
  })

  it('uses native smooth scrolling at both distances and auto with reduced motion', async () => {
    const far = await mountMinimap()
    markers(far.host)[7].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    expect(far.thread.scrollTo).toHaveBeenLastCalledWith({
      top: far.thread.offsets[7] - 16,
      behavior: 'smooth',
    })

    vi.stubGlobal('matchMedia', vi.fn((query: string) => (
      mediaQueryList(query, query === '(prefers-reduced-motion: reduce)')
    )))
    const reduced = await mountMinimap()
    markers(reduced.host)[1].dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    expect(reduced.thread.scrollTo).toHaveBeenLastCalledWith({
      top: reduced.thread.offsets[1] - 16,
      behavior: 'auto',
    })
    reduced.thread.container.dispatchEvent(new Event('scrollend'))
    expect(reduced.thread.container.querySelector('[data-chat-turn-key="user-1"]')?.classList.contains('is-history-target')).toBe(true)
  })

  it('tracks the current prompt and supports roving keyboard focus', async () => {
    const { host, thread } = await mountMinimap()
    thread.container.scrollTop = 1000
    thread.container.dispatchEvent(new Event('scroll'))
    await vi.waitFor(() => expect(markers(host)[2].getAttribute('aria-current')).toBe('location'))

    const active = markers(host)[2]
    active.focus()
    active.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()

    const next = markers(host)[3]
    expect(document.activeElement).toBe(next)
    expect(next.tabIndex).toBe(0)
    expect(Number(
      next.style.getPropertyValue('--conversation-minimap-line-scale-x'),
    )).toBe(1)
    expect(Number(
      markers(host)[2].style.getPropertyValue('--conversation-minimap-line-scale-x'),
    )).toBeGreaterThan(0.2667)
    expect(Number(
      markers(host)[2].style.getPropertyValue('--conversation-minimap-line-scale-y'),
    )).toBe(1)
    expect(Number(
      next.style.getPropertyValue('--conversation-minimap-line-scale-y'),
    )).toBe(0.5)
    next.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(thread.scrollTo).toHaveBeenLastCalledWith({ top: thread.offsets[3] - 16, behavior: 'smooth' })
    expect(document.activeElement).toBe(thread.container.querySelector('[data-chat-turn-key="user-3"]'))
  })

  it('labels the loaded range without exposing a manual load-earlier control', async () => {
    const { host } = await mountMinimap(8, { historyHasMore: true })

    expect(host.querySelector('nav')?.getAttribute('aria-label')).toContain('earlier messages available')
    expect(markers(host)[0].getAttribute('aria-label')).toContain('Loaded prompt 1 of 8')
    expect(host.querySelector('[data-testid="conversation-minimap-load-earlier"]')).toBeNull()
  })

  it('keeps a focused prompt keyed correctly when earlier history is prepended', async () => {
    const initialMessages = messages(8)
    delete initialMessages[4].id
    delete initialMessages[4].messageId
    initialMessages[4].clientId = 'local-user-stable'
    const messageState = ref(initialMessages)
    const thread = makeThread(initialMessages)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const Root = defineComponent(() => () => h(ConversationMinimap, {
      messages: messageState.value,
      scrollContainer: thread.container,
      stripTimePrefix: (value: string) => value,
    }))
    const app = createApp(Root)
    app.use(i18n)
    app.mount(host)
    mountedApps.push(app)
    await vi.waitFor(() => expect(markers(host)).toHaveLength(8))

    markers(host)[2].focus()
    await nextTick()
    expect(host.querySelector('[role="tooltip"]')?.textContent).toContain('prompt 2')

    messageState.value = [message('user', 99), message('assistant', 99), ...initialMessages]
    await nextTick()
    await nextTick()

    expect(markers(host)).toHaveLength(9)
    expect(host.querySelector('[role="tooltip"]')?.textContent).toContain('prompt 2')
    expect(markers(host)[3].tabIndex).toBe(0)
    expect(document.activeElement).toBe(markers(host)[3])
  })

  it('stays hidden below the prompt and scroll-range thresholds', async () => {
    const rendered = messages(7)
    const thread = makeThread(rendered)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ConversationMinimap, {
      messages: rendered,
      scrollContainer: thread.container,
      stripTimePrefix: (value: string) => value,
    })
    app.use(i18n)
    app.mount(host)
    mountedApps.push(app)
    await new Promise(resolve => window.setTimeout(resolve, 20))

    expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull()

    const nonScrollingMessages = messages(8)
    const nonScrollingThread = makeThread(nonScrollingMessages, 1200, { scrollHeight: 1499 })
    const nonScrollingHost = document.createElement('div')
    document.body.appendChild(nonScrollingHost)
    const nonScrollingApp = createApp(ConversationMinimap, {
      messages: nonScrollingMessages,
      scrollContainer: nonScrollingThread.container,
      stripTimePrefix: (value: string) => value,
    })
    nonScrollingApp.use(i18n)
    nonScrollingApp.mount(nonScrollingHost)
    mountedApps.push(nonScrollingApp)
    await new Promise(resolve => window.setTimeout(resolve, 20))

    expect(nonScrollingHost.querySelector('[data-testid="conversation-minimap"]')).toBeNull()
  })

  it('appears at eight prompts and 1.5 viewports of scrollable distance', async () => {
    const { host } = await mountMinimap(8, {}, { scrollHeight: 1500 })
    expect(markers(host)).toHaveLength(8)
  })

  it('enters only at the 1120px conversation-pane threshold', async () => {
    const rendered = messages(8)
    const narrowThread = makeThread(rendered, 1119)
    const narrowHost = document.createElement('div')
    document.body.appendChild(narrowHost)
    const narrowApp = createApp(ConversationMinimap, {
      messages: rendered,
      scrollContainer: narrowThread.container,
      stripTimePrefix: (value: string) => value,
    })
    narrowApp.use(i18n)
    narrowApp.mount(narrowHost)
    mountedApps.push(narrowApp)
    await new Promise(resolve => window.setTimeout(resolve, 20))

    expect(narrowHost.querySelector('[data-testid="conversation-minimap"]')).toBeNull()
    const wide = await mountMinimap(8, {}, { clientWidth: 1120 })
    expect(markers(wide.host)).toHaveLength(8)
  })

  it('keeps the rail mounted until the pane crosses the 1104px collision floor', async () => {
    const observers = stubResizeObservers()
    const { host, thread } = await mountMinimap(8, {}, { clientWidth: 1120 })
    const shellObserver = observers.find(observer => observer.targets.has(thread.container))!
    const resizeTo = async (width: number) => {
      Object.defineProperty(thread.container, 'clientWidth', { configurable: true, value: width })
      shellObserver.callback([], shellObserver as unknown as ResizeObserver)
      await nextTick()
    }

    await resizeTo(1105)
    expect(markers(host)).toHaveLength(8)
    await resizeTo(1104)
    await vi.waitFor(() => expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull())
    await resizeTo(1119)
    expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull()
    await resizeTo(1120)
    await vi.waitFor(() => expect(markers(host)).toHaveLength(8))
  })

  it('uses a lower exit threshold so small layout changes do not flicker the rail', async () => {
    const observers = stubResizeObservers()
    const { host, thread } = await mountMinimap(8, {}, { scrollHeight: 1500 })
    const threadObserver = observers.find(observer => observer.targets.size > 1)!
    const resizeThread = async (scrollHeight: number) => {
      Object.defineProperty(thread.container, 'scrollHeight', { configurable: true, value: scrollHeight })
      threadObserver.callback(
        [{ target: thread.container } as unknown as ResizeObserverEntry],
        threadObserver as unknown as ResizeObserver,
      )
      await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))
      await nextTick()
    }

    await resizeThread(1200)
    expect(markers(host)).toHaveLength(8)

    await resizeThread(1199)
    expect(
      host.querySelector('[data-testid="conversation-minimap"]')
        ?.classList.contains('conversation-minimap-shell-leave-active'),
    ).toBe(true)
  })

  it('resets threshold hysteresis when the session changes even if fallback keys overlap', async () => {
    const initialMessages = messages(8).map(entry => ({ ...entry, id: undefined, messageId: undefined }))
    const nextMessages = initialMessages.map(entry => ({
      ...entry,
      text: `${entry.text} in another session`,
    }))
    const messageState = ref(initialMessages)
    const sessionKey = ref('agent:session-a')
    const thread = makeThread(initialMessages, 1200, { scrollHeight: 1500 })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const Root = defineComponent(() => () => h(ConversationMinimap, {
      messages: messageState.value,
      scrollContainer: thread.container,
      stripTimePrefix: (value: string) => value,
      sessionKey: sessionKey.value,
    }))
    const app = createApp(Root)
    app.use(i18n)
    app.mount(host)
    mountedApps.push(app)
    await vi.waitFor(() => expect(markers(host)).toHaveLength(8))

    Object.defineProperty(thread.container, 'scrollHeight', { configurable: true, value: 1200 })
    messageState.value = nextMessages
    sessionKey.value = 'agent:session-b'

    await vi.waitFor(() => expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull())
  })

  it('does not scan prompt anchors while the conversation pane is narrow', async () => {
    const rendered = messages(8)
    const thread = makeThread(rendered, 800)
    const queryAnchors = vi.spyOn(thread.container, 'querySelectorAll')
    const stripTimePrefix = vi.fn((value: string) => value)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ConversationMinimap, {
      messages: rendered,
      scrollContainer: thread.container,
      stripTimePrefix,
    })
    app.use(i18n)
    app.mount(host)
    mountedApps.push(app)
    await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))

    expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull()
    expect(queryAnchors).not.toHaveBeenCalled()
    expect(stripTimePrefix).not.toHaveBeenCalled()
  })

  it('keeps the desktop rail disabled on a coarse-only touch surface', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => mediaQueryList(
      query,
      query === '(hover: none) and (pointer: coarse)',
    )))
    const rendered = messages(8)
    const thread = makeThread(rendered, 1200)
    const stripTimePrefix = vi.fn((value: string) => value)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ConversationMinimap, {
      messages: rendered,
      scrollContainer: thread.container,
      stripTimePrefix,
    })
    app.use(i18n)
    app.mount(host)
    mountedApps.push(app)
    await new Promise(resolve => window.setTimeout(resolve, 20))

    expect(host.querySelector('[data-testid="conversation-minimap"]')).toBeNull()
    expect(stripTimePrefix).not.toHaveBeenCalled()
  })
})
