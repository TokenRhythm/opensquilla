// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App, type ComponentPublicInstance } from 'vue'
import { createI18n } from 'vue-i18n'

import ChatHeaderActions from './ChatHeaderActions.vue'

type LayoutName = 'wide' | 'compact' | 'tight'
type Action = 'deliverables' | 'share' | 'copy-session-key' | 'copy-session-link' | 'copy-gateway-link'

type HeaderInstance = ComponentPublicInstance & {
  focusAction: (action: Action) => boolean
}

const BASE_PROPS = {
  title: 'Responsive header test',
  copyState: null,
  copyIcon: 'copy' as const,
  copyLiveText: '',
  deliverableCount: 2,
  hasNewDeliverable: false,
  shareMode: false,
  shareableMessageCount: 3,
}

const messages = {
  chat: {
    copied: 'Copied',
    copySessionKey: 'Copy session ID',
    copySessionAppLink: 'Copy application link',
    copySessionGatewayLink: 'Copy Gateway web link',
    deliverables: 'Deliverables',
    deliverablesCount: 'Deliverables ({count})',
    sessionActions: 'Session actions',
    share: 'Share',
    shareSelectHint: 'Select messages to share',
    shareSendFirst: 'Send a message first to share',
  },
}

const LAYOUT_CASES: Array<{ layout: LayoutName; width: number }> = [
  { layout: 'wide', width: 800 },
  { layout: 'compact', width: 400 },
  { layout: 'tight', width: 120 },
]

const ACTION_STATES = [
  { deliverableCount: 0, shareMode: false, shareableMessageCount: 0 },
  { deliverableCount: 0, shareMode: false, shareableMessageCount: 3 },
  { deliverableCount: 0, shareMode: true, shareableMessageCount: 0 },
  { deliverableCount: 0, shareMode: true, shareableMessageCount: 3 },
  { deliverableCount: 2, shareMode: false, shareableMessageCount: 0 },
  { deliverableCount: 2, shareMode: false, shareableMessageCount: 3 },
  { deliverableCount: 2, shareMode: true, shareableMessageCount: 0 },
  { deliverableCount: 2, shareMode: true, shareableMessageCount: 3 },
]

const ACTION_TRUTH_TABLE = LAYOUT_CASES.flatMap(layout =>
  ACTION_STATES.map(state => ({ ...layout, ...state })))

const mounted: Array<{ app: App; el: HTMLElement }> = []
let headerWidth = 800
let coarsePointer = false
let animationFrameId = 0
const animationFrames = new Map<number, FrameRequestCallback>()
const coarsePointerListeners = new Set<EventListenerOrEventListenerObject>()

interface ResizeObserverFixture {
  callback: ResizeObserverCallback
  disconnect: ReturnType<typeof vi.fn>
  observe: ReturnType<typeof vi.fn>
  unobserve: ReturnType<typeof vi.fn>
}

const resizeObservers: ResizeObserverFixture[] = []

function rect(width: number, height = 48): DOMRect {
  return {
    x: 0,
    y: 0,
    top: 0,
    right: width,
    bottom: height,
    left: 0,
    width,
    height,
    toJSON: () => ({}),
  } as DOMRect
}

async function flush() {
  await nextTick()
  await nextTick()
}

async function flushAnimationFrame() {
  const callbacks = Array.from(animationFrames.values())
  animationFrames.clear()
  callbacks.forEach(callback => callback(0))
  await flush()
}

function resizeHeader(width: number, observer = resizeObservers[resizeObservers.length - 1]!) {
  headerWidth = width
  observer.callback([], observer as unknown as ResizeObserver)
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  window.dispatchEvent(new Event('resize'))
}

function setCoarsePointer(matches: boolean) {
  coarsePointer = matches
  const event = { matches, media: '(pointer: coarse)' } as MediaQueryListEvent
  for (const listener of coarsePointerListeners) {
    if (typeof listener === 'function') listener(event)
    else listener.handleEvent(event)
  }
}

async function mountHeader(
  width: number,
  overrides: Partial<typeof BASE_PROPS> = {},
) {
  headerWidth = width
  const handlers = {
    deliverables: vi.fn(),
    share: vi.fn(),
    copy: vi.fn(),
    copyLink: vi.fn(),
    copyGatewayLink: vi.fn(),
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatHeaderActions, {
    ...BASE_PROPS,
    ...overrides,
    onOpenDeliverables: handlers.deliverables,
    onStartShare: handlers.share,
    onCopySessionKey: handlers.copy,
    onCopySessionLink: handlers.copyLink,
    onCopyGatewayLink: handlers.copyGatewayLink,
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en: messages },
  }))
  const instance = app.mount(el) as HeaderInstance
  mounted.push({ app, el })
  const observer = resizeObservers[resizeObservers.length - 1]!
  await flush()
  return { app, el, handlers, instance, observer }
}

function trigger(el: HTMLElement): HTMLButtonElement {
  return el.querySelector<HTMLButtonElement>('[data-testid="chat-session-actions-trigger"]')!
}

async function openMenu(el: HTMLElement) {
  trigger(el).click()
  await flush()
}

function renderedActions(el: HTMLElement): string[] {
  const actions: string[] = []
  for (const node of el.querySelectorAll<HTMLElement>('[data-action]')) {
    actions.push(node.dataset.action!)
  }
  for (const node of el.querySelectorAll<HTMLElement>('[data-testid^="chat-session-action-"]')) {
    const action = node.dataset.testid!.replace('chat-session-action-', '')
    actions.push(action === 'copy' ? 'copy-session-key' : action === 'copy-app-link' ? 'copy-session-link' : action)
  }
  return actions
}

function expectedActions({
  deliverableCount,
  shareMode,
}: Pick<typeof BASE_PROPS, 'deliverableCount' | 'shareMode'>): Action[] {
  const actions: Action[] = []
  if (deliverableCount > 0) actions.push('deliverables')
  if (!shareMode) actions.push('share')
  actions.push('copy-session-key')
  actions.push('copy-session-link')
  actions.push('copy-gateway-link')
  return actions
}

function expectedPrimaryAction({
  layout,
  deliverableCount,
  shareMode,
  shareableMessageCount,
}: {
  layout: LayoutName
  deliverableCount: number
  shareMode: boolean
  shareableMessageCount: number
}): Action | null {
  if (layout !== 'compact') return null
  if (deliverableCount > 0) return 'deliverables'
  if (!shareMode && shareableMessageCount > 0) return 'share'
  return null
}

beforeEach(() => {
  document.body.innerHTML = ''
  headerWidth = 800
  coarsePointer = false
  animationFrameId = 0
  animationFrames.clear()
  coarsePointerListeners.clear()
  resizeObservers.length = 0
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 })

  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return rect(this.matches('[data-testid="chat-header-actions"]') ? headerWidth : 44)
  })
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    const bounds = this.getBoundingClientRect()
    return [bounds] as unknown as DOMRectList
  })
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
    const id = ++animationFrameId
    animationFrames.set(id, callback)
    return id
  }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => {
    animationFrames.delete(id)
  }))
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    get matches() {
      return query === '(pointer: coarse)' && coarsePointer
    },
    media: query,
    onchange: null,
    addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
      coarsePointerListeners.add(listener)
    },
    removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
      coarsePointerListeners.delete(listener)
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  } as MediaQueryList)))
  vi.stubGlobal('ResizeObserver', class implements ResizeObserverFixture {
    callback: ResizeObserverCallback
    disconnect = vi.fn()
    observe = vi.fn()
    unobserve = vi.fn()

    constructor(callback: ResizeObserverCallback) {
      this.callback = callback
      resizeObservers.push(this)
    }
  })
})

afterEach(() => {
  while (mounted.length) {
    const { app, el } = mounted.pop()!
    app.unmount()
    el.remove()
  }
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ChatHeaderActions', () => {
  it('exposes the full title and provides the same session menu in wide layouts', async () => {
    const fullTitle = 'A complete session title that may be visually truncated'
    const { el } = await mountHeader(800, { title: fullTitle })
    const identity = el.querySelector<HTMLElement>('.chat-header__identity')!
    const title = identity.querySelector<HTMLHeadingElement>('.chat-header__title')!
    const spacer = el.querySelector<HTMLElement>('.chat-header__spacer')!

    expect(title.textContent).toBe(fullTitle)
    expect(title.title).toBe(fullTitle)
    expect(trigger(el).getAttribute('aria-haspopup')).toBe('menu')
    await openMenu(el)
    expect(Array.from(el.querySelectorAll('[role="menuitem"]')).map(item => item.textContent?.trim()))
      .toEqual(['Copy application link', 'Copy Gateway web link', 'Copy session ID'])
    expect(identity.nextElementSibling).toBe(spacer)
    expect(spacer.nextElementSibling?.classList.contains('chat-header__actions')).toBe(true)
  })

  it('keeps internal workbench resources out of the session header', async () => {
    const { el } = await mountHeader(800)
    const deliverables = el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-session-action-deliverables"]',
    )
    expect(el.querySelector('[data-testid="chat-session-action-workbench"]')).toBeNull()
    expect(el.textContent).not.toContain('Workbench')
    expect(deliverables?.getAttribute('aria-label')).toBe('Deliverables (2)')
    expect(deliverables?.querySelector('.chat-header__count-badge')?.textContent).toBe('2')
  })

  it('shows a small New marker without replacing the deliverable count', async () => {
    const { el } = await mountHeader(800, { hasNewDeliverable: true })
    const deliverables = el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-session-action-deliverables"]',
    )!

    expect(deliverables.querySelector('[data-testid="chat-deliverables-new-badge"]')?.textContent)
      .toBe('New')
    expect(deliverables.querySelector('.chat-header__count-badge')?.textContent).toBe('2')
    expect(deliverables.getAttribute('aria-label')).toBe('Deliverables (2), New')
  })

  it.each([
    { width: 143, expected: 'tight' },
    { width: 184, expected: 'compact' },
    { width: 216, expected: 'compact' },
    { width: 544, expected: 'compact' },
    { width: 576, expected: 'wide' },
  ])('classifies an initial $width px host as $expected without waiting for a frame', async ({
    width,
    expected,
  }) => {
    const { el } = await mountHeader(width)

    expect(el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')?.dataset.layout)
      .toBe(expected)
    expect(requestAnimationFrame).not.toHaveBeenCalled()
  })

  it('applies both hysteresis bands at their exact transition boundaries', async () => {
    const { el, observer } = await mountHeader(800)
    const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!

    for (const [width, expected] of [
      [544, 'wide'],
      [543, 'compact'],
      [184, 'compact'],
      [183, 'tight'],
      [215, 'tight'],
      [216, 'compact'],
      [575, 'compact'],
      [576, 'wide'],
    ] as const) {
      resizeHeader(width, observer)
      await flushAnimationFrame()
      expect(header.dataset.layout, `${width}px`).toBe(expected)
    }
  })

  it('coalesces repeated ResizeObserver notifications into one animation frame', async () => {
    const { el, observer } = await mountHeader(800)
    const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!

    resizeHeader(700, observer)
    resizeHeader(600, observer)
    resizeHeader(500, observer)

    expect(requestAnimationFrame).toHaveBeenCalledTimes(1)
    expect(header.dataset.layout).toBe('wide')
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('compact')
  })

  it('caps wide on mobile and coarse pointers while still allowing tight', async () => {
    const { el, observer } = await mountHeader(800)
    const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!

    setViewportWidth(768)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('compact')

    setViewportWidth(1200)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('wide')

    setCoarsePointer(true)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('compact')

    resizeHeader(143, observer)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('tight')

    resizeHeader(800, observer)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('compact')

    setCoarsePointer(false)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('wide')
  })

  it('keeps focus reachable and closes the menu across wide, compact, tight, and wide', async () => {
    const { el, observer } = await mountHeader(800)
    const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!
    trigger(el).focus()

    resizeHeader(543, observer)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('compact')
    expect(document.activeElement).toBe(trigger(el))

    await openMenu(el)
    const menuCopy = el.querySelector<HTMLButtonElement>('[data-testid="chat-session-action-copy"]')!
    menuCopy.focus()

    resizeHeader(183, observer)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('tight')
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(trigger(el))

    resizeHeader(576, observer)
    await flushAnimationFrame()
    expect(header.dataset.layout).toBe('wide')
    expect(document.activeElement).toBe(trigger(el))
  })

  it('disconnects observers, media listeners, and a pending frame on unmount', async () => {
    const { app, el, observer } = await mountHeader(800)
    expect(observer.observe).toHaveBeenCalledWith(
      el.querySelector('[data-testid="chat-header-actions"]'),
    )
    expect(coarsePointerListeners.size).toBe(1)

    resizeHeader(500, observer)
    expect(animationFrames.size).toBe(1)
    mounted.pop()
    app.unmount()
    el.remove()

    expect(observer.disconnect).toHaveBeenCalledTimes(1)
    expect(cancelAnimationFrame).toHaveBeenCalledTimes(1)
    expect(animationFrames.size).toBe(0)
    expect(coarsePointerListeners.size).toBe(0)

    const scheduledFrames = vi.mocked(requestAnimationFrame).mock.calls.length
    setViewportWidth(700)
    setCoarsePointer(true)
    expect(requestAnimationFrame).toHaveBeenCalledTimes(scheduledFrames)
  })

  it.each(ACTION_TRUTH_TABLE)(
    'renders the exact action truth table in $layout with deliverables=$deliverableCount, shareMode=$shareMode, shareable=$shareableMessageCount',
    async ({ layout, width, deliverableCount, shareMode, shareableMessageCount }) => {
      const state = { deliverableCount, shareMode, shareableMessageCount }
      const { el } = await mountHeader(width, state)
      const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!
      expect(header.dataset.layout).toBe(layout)

      const expectedPrimary = expectedPrimaryAction({ layout, ...state })
      const primary = el.querySelector<HTMLElement>('[data-action]')
      expect(primary?.dataset.action ?? null).toBe(expectedPrimary)

      await openMenu(el)

      const expected = expectedActions(state)
      const actions = renderedActions(el)
      expect(actions.sort()).toEqual([...expected].sort())
      expect(new Set(actions).size).toBe(actions.length)

      const menuActions = layout === 'wide'
        ? []
        : expected.filter(action => action !== expectedPrimary)
      expect(Boolean(el.querySelector('[role="separator"]'))).toBe(menuActions.some(action => action === 'share' || action === 'deliverables'))

      const tightBadge = layout === 'wide'
        ? null
        : trigger(el).querySelector<HTMLElement>('.chat-header__count-badge')
      expect(Boolean(tightBadge)).toBe(layout === 'tight' && deliverableCount > 0)
      if (tightBadge) {
        expect(tightBadge.textContent).toBe('2')
        expect(tightBadge.getAttribute('aria-hidden')).toBe('true')
        expect(trigger(el).getAttribute('aria-label')).toBe('Session actions')
      }

      if (!shareMode && shareableMessageCount === 0) {
        const unavailableShare = el.querySelector<HTMLButtonElement>(
          '[data-action="share"], [data-testid="chat-session-action-share"]',
        )!
        expect(unavailableShare.disabled).toBe(false)
        expect(unavailableShare.tabIndex).toBe(0)
        expect(unavailableShare.getAttribute('aria-disabled')).toBe('true')
      }
    },
  )

  it.each([
    { count: 99, badge: '99' },
    { count: 100, badge: '99+' },
  ])('shows a neutral $badge count while preserving the full accessible count', async ({
    count,
    badge,
  }) => {
    const wide = await mountHeader(800, { deliverableCount: count })
    const wideAction = wide.el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-session-action-deliverables"]',
    )!
    expect(wideAction.classList.contains('topbar-state--deliverables')).toBe(true)
    expect(wideAction.dataset.state).toBe('normal')
    expect(wideAction.getAttribute('aria-label')).toBe(`Deliverables (${count})`)
    expect(wideAction.querySelector('.chat-header__count-badge')?.textContent).toBe(badge)

    const compact = await mountHeader(400, { deliverableCount: count })
    const compactAction = compact.el.querySelector<HTMLButtonElement>(
      '[data-action="deliverables"]',
    )!
    expect(compactAction.classList.contains('topbar-state--deliverables')).toBe(true)
    expect(compactAction.dataset.state).toBe('normal')
    expect(compactAction.getAttribute('aria-label')).toBe(`Deliverables (${count})`)
    expect(compactAction.querySelector('.chat-header__count-badge')?.textContent).toBe(badge)

    const tight = await mountHeader(120, { deliverableCount: count })
    const tightBadge = trigger(tight.el).querySelector<HTMLElement>('.chat-header__count-badge')!
    expect(tightBadge.classList.contains('topbar-state--deliverables')).toBe(true)
    expect(tightBadge.dataset.state).toBe('normal')
    expect(tightBadge.textContent).toBe(badge)
    expect(tightBadge.getAttribute('aria-hidden')).toBe('true')
    expect(trigger(tight.el).getAttribute('aria-label')).toBe('Session actions')

    await openMenu(tight.el)
    const menuAction = tight.el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-session-action-deliverables"]',
    )!
    expect(menuAction.classList.contains('topbar-state--deliverables')).toBe(true)
    expect(menuAction.dataset.state).toBe('normal')
    expect(menuAction.getAttribute('aria-label')).toBe(`Deliverables (${count})`)
    expect(menuAction.querySelector('.chat-header__count-badge')?.textContent).toBe(badge)
  })

  it('keeps unavailable wide share focusable and exposes its exact reason', async () => {
    const { el, handlers, instance } = await mountHeader(800, {
      deliverableCount: 0,
      shareableMessageCount: 0,
    })
    const share = el.querySelector<HTMLButtonElement>('[data-testid="chat-session-action-share"]')!

    expect(share.disabled).toBe(false)
    expect(share.tabIndex).toBe(0)
    expect(share.getAttribute('aria-disabled')).toBe('true')
    expect(share.getAttribute('aria-label')).toBe('Send a message first to share')
    expect(share.title).toBe('Send a message first to share')

    share.focus()
    expect(document.activeElement).toBe(share)
    expect(instance.focusAction('share')).toBe(true)
    expect(document.activeElement).toBe(share)

    share.click()
    expect(handlers.share).not.toHaveBeenCalled()
  })

  it('keeps the compact deliverables priority and tight menu placement explicit', async () => {
    for (const { layout, width } of LAYOUT_CASES) {
      const { el } = await mountHeader(width)
      const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!
      expect(header.dataset.layout).toBe(layout)

      if (layout !== 'wide') await openMenu(el)

      if (layout === 'compact') {
        expect(el.querySelector('[data-action="deliverables"]')).toBeTruthy()
        expect(el.querySelector('[data-testid="chat-session-action-deliverables"]')).toBeNull()
      }
      if (layout === 'tight') {
        expect(el.querySelector('[data-action]')).toBeNull()
        expect(el.querySelector('[data-testid="chat-session-action-deliverables"]')).toBeTruthy()
      }
    }
  })

  it.each(LAYOUT_CASES)('supports keyboard navigation and restores trigger focus in $layout', async ({ width, layout }) => {
    const { el } = await mountHeader(width)
    const menuTrigger = trigger(el)
    menuTrigger.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowDown',
      bubbles: true,
    }))
    await flush()

    const items = Array.from(el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    expect(items).toHaveLength(layout === 'wide' ? 3 : layout === 'tight' ? 5 : 4)
    expect(el.querySelector('[data-chat-topbar-popover="session-actions"]')).toBeTruthy()
    expect(document.activeElement).toBe(items[0])

    items[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(document.activeElement).toBe(items[1])

    items[1]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(items[items.length - 1])

    items[items.length - 1]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(menuTrigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(menuTrigger)
  })

  it('closes the session menu on outside click without stealing outside focus', async () => {
    const { el } = await mountHeader(400)
    await openMenu(el)
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    outside.click()
    await flush()

    expect(el.querySelector('[data-chat-topbar-popover="session-actions"]')).toBeNull()
    expect(document.activeElement).toBe(outside)
  })

  it('keeps the menu open and emits nothing when disabled share is activated', async () => {
    const { el, handlers } = await mountHeader(400, {
      deliverableCount: 0,
      shareableMessageCount: 0,
    })
    await openMenu(el)

    const share = el.querySelector<HTMLButtonElement>('[data-testid="chat-session-action-share"]')!
    expect(share.getAttribute('aria-disabled')).toBe('true')
    share.click()
    await flush()

    expect(handlers.share).not.toHaveBeenCalled()
    expect(el.querySelector('[role="menu"]')).toBeTruthy()
    expect(trigger(el).getAttribute('aria-expanded')).toBe('true')
  })

  it.each(LAYOUT_CASES)('emits each action once from $layout controls', async ({ width, layout }) => {
    const { el, handlers } = await mountHeader(width)

    if (layout === 'tight') await openMenu(el)
    el.querySelector<HTMLButtonElement>('[data-action="deliverables"], [data-testid="chat-session-action-deliverables"]')!.click()
    await flush()
    expect(handlers.deliverables).toHaveBeenCalledTimes(1)

    for (const [testId, handler] of [
      ...(layout === 'wide' ? [] : [['chat-session-action-share', handlers.share]] as const),
      ['chat-session-action-copy', handlers.copy],
      ['chat-session-action-copy-app-link', handlers.copyLink],
      ['chat-session-action-copy-gateway-link', handlers.copyGatewayLink],
    ] as const) {
      await openMenu(el)
      el.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`)!.click()
      await flush()
      expect(handler).toHaveBeenCalledTimes(1)
      expect(el.querySelector('[role="menu"]')).toBeNull()
    }

    if (layout === 'wide') {
      el.querySelector<HTMLButtonElement>('[data-testid="chat-session-action-share"]')!.click()
    }
    expect(handlers.deliverables).toHaveBeenCalledTimes(1)
    expect(handlers.share).toHaveBeenCalledTimes(1)
    expect(handlers.copy).toHaveBeenCalledTimes(1)
    expect(handlers.copyLink).toHaveBeenCalledTimes(1)
    expect(handlers.copyGatewayLink).toHaveBeenCalledTimes(1)
  })

  it.each(LAYOUT_CASES)('keeps all copy actions reachable in $layout', async ({ width }) => {
    const { el, instance } = await mountHeader(width)
    for (const action of ['copy-session-key', 'copy-session-link', 'copy-gateway-link'] as const) {
      expect(instance.focusAction(action)).toBe(true)
      expect(document.activeElement).toBe(trigger(el))
    }
  })

  it('focusAction targets direct controls and falls back to the compact menu trigger', async () => {
    const { el, instance } = await mountHeader(400)
    const primary = el.querySelector<HTMLButtonElement>('[data-action="deliverables"]')!
    const menuTrigger = trigger(el)

    expect(instance.focusAction('deliverables')).toBe(true)
    expect(document.activeElement).toBe(primary)

    expect(instance.focusAction('share')).toBe(true)
    expect(document.activeElement).toBe(menuTrigger)

    const tight = await mountHeader(120)
    expect(tight.instance.focusAction('deliverables')).toBe(true)
    expect(document.activeElement).toBe(trigger(tight.el))
  })
})
