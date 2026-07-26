// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App, type ComponentPublicInstance } from 'vue'
import { createI18n } from 'vue-i18n'

import ChatHeaderActions from './ChatHeaderActions.vue'

type Action = 'deliverables' | 'runs' | 'share' | 'copy-session-key'

type HeaderInstance = ComponentPublicInstance & {
  focusAction: (action: Action) => boolean
}

const BASE_PROPS = {
  title: 'Responsive header test',
  sessionKey: 'session-test-123',
  copyState: null,
  copyIcon: 'copy' as const,
  copyLiveText: '',
  deliverableCount: 2,
  runHistoryVisible: true,
  shareMode: false,
  shareableMessageCount: 3,
}

const messages = {
  chat: {
    copied: 'Copied',
    copySessionKey: 'Copy session key',
    deliverablesCount: 'Deliverables ({count})',
    metaRunHistory: 'Run history',
    runs: 'Runs',
    sessionActions: 'Session actions',
    share: 'Share',
    shareSelectHint: 'Select messages to share',
    shareSendFirst: 'Send a message first to share',
  },
}

const mounted: Array<{ app: App; el: HTMLElement }> = []
let headerWidth = 800

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

async function mountHeader(
  width: number,
  overrides: Partial<typeof BASE_PROPS> = {},
) {
  headerWidth = width
  const handlers = {
    deliverables: vi.fn(),
    runs: vi.fn(),
    share: vi.fn(),
    copy: vi.fn(),
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatHeaderActions, {
    ...BASE_PROPS,
    ...overrides,
    onOpenDeliverables: handlers.deliverables,
    onOpenRunHistory: handlers.runs,
    onStartShare: handlers.share,
    onCopySessionKey: handlers.copy,
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en: messages },
  }))
  const instance = app.mount(el) as HeaderInstance
  mounted.push({ app, el })
  await flush()
  return { el, handlers, instance }
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
  if (el.querySelector('.chat-header__copy')) actions.push('copy-session-key')
  for (const node of el.querySelectorAll<HTMLElement>('[data-action]')) {
    actions.push(node.dataset.action!)
  }
  for (const node of el.querySelectorAll<HTMLElement>('[data-testid^="chat-session-action-"]')) {
    const action = node.dataset.testid!.replace('chat-session-action-', '')
    actions.push(action === 'copy' ? 'copy-session-key' : action)
  }
  return actions
}

beforeEach(() => {
  document.body.innerHTML = ''
  headerWidth = 800
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 })

  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return rect(this.matches('[data-testid="chat-header-actions"]') ? headerWidth : 44)
  })
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    const bounds = this.getBoundingClientRect()
    return [bounds] as unknown as DOMRectList
  })
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
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
  it.each([
    { name: 'wide', width: 800 },
    { name: 'compact', width: 400 },
    { name: 'tight', width: 120 },
  ])('renders every available action exactly once in the $name layout', async ({ name, width }) => {
    const { el } = await mountHeader(width)
    const header = el.querySelector<HTMLElement>('[data-testid="chat-header-actions"]')!
    expect(header.dataset.layout).toBe(name)

    if (name !== 'wide') await openMenu(el)

    const actions = renderedActions(el)
    expect(actions.sort()).toEqual(['copy-session-key', 'deliverables', 'runs', 'share'].sort())
    expect(new Set(actions).size).toBe(actions.length)

    if (name === 'compact') {
      expect(el.querySelector('[data-action="deliverables"]')).toBeTruthy()
      expect(el.querySelector('[data-testid="chat-session-action-deliverables"]')).toBeNull()
    }
    if (name === 'tight') {
      expect(el.querySelector('[data-action]')).toBeNull()
      expect(el.querySelector('[data-testid="chat-session-action-deliverables"]')).toBeTruthy()
    }
  })

  it('supports menu arrow navigation and restores trigger focus on Escape', async () => {
    const { el } = await mountHeader(400)
    const menuTrigger = trigger(el)
    menuTrigger.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowDown',
      bubbles: true,
    }))
    await flush()

    const items = Array.from(el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    expect(items).toHaveLength(3)
    expect(document.activeElement).toBe(items[0])

    items[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(document.activeElement).toBe(items[1])

    items[1]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(items[2])

    items[2]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(menuTrigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(menuTrigger)
  })

  it('keeps the menu open and emits nothing when disabled share is activated', async () => {
    const { el, handlers } = await mountHeader(400, {
      deliverableCount: 0,
      runHistoryVisible: false,
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

  it('emits each action once from compact primary and menu controls', async () => {
    const { el, handlers } = await mountHeader(400)

    el.querySelector<HTMLButtonElement>('[data-action="deliverables"]')!.click()
    expect(handlers.deliverables).toHaveBeenCalledTimes(1)

    for (const [testId, handler] of [
      ['chat-session-action-runs', handlers.runs],
      ['chat-session-action-share', handlers.share],
      ['chat-session-action-copy', handlers.copy],
    ] as const) {
      await openMenu(el)
      el.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`)!.click()
      await flush()
      expect(handler).toHaveBeenCalledTimes(1)
      expect(el.querySelector('[role="menu"]')).toBeNull()
    }

    expect(handlers.deliverables).toHaveBeenCalledTimes(1)
    expect(handlers.runs).toHaveBeenCalledTimes(1)
    expect(handlers.share).toHaveBeenCalledTimes(1)
    expect(handlers.copy).toHaveBeenCalledTimes(1)
  })

  it('focusAction targets direct controls and falls back to the compact menu trigger', async () => {
    const { el, instance } = await mountHeader(400)
    const primary = el.querySelector<HTMLButtonElement>('[data-action="deliverables"]')!
    const menuTrigger = trigger(el)

    expect(instance.focusAction('deliverables')).toBe(true)
    expect(document.activeElement).toBe(primary)

    expect(instance.focusAction('share')).toBe(true)
    expect(document.activeElement).toBe(menuTrigger)

    expect(instance.focusAction('runs')).toBe(true)
    expect(document.activeElement).toBe(menuTrigger)

    const tight = await mountHeader(120)
    expect(tight.instance.focusAction('deliverables')).toBe(true)
    expect(document.activeElement).toBe(trigger(tight.el))
  })
})
