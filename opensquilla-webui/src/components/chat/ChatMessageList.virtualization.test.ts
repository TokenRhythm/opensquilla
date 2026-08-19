// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'

import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import type { ChatMessageListVirtualizer } from '@/utils/chat/variableMessageWindow'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []
let resizeObserverCallbacks: ResizeObserverCallback[] = []

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
    constructor(callback: ResizeObserverCallback) {
      resizeObserverCallbacks.push(callback)
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  })
}

async function mountList(options: {
  shareMode?: boolean
  forceMountMessageKeys?: ReadonlySet<string>
  followLiveEdge?: boolean
  messages?: ChatRenderedMessage[]
} = {}) {
  const container = document.createElement('div')
  const host = document.createElement('div')
  container.appendChild(host)
  document.body.appendChild(container)
  Object.defineProperties(container, {
    clientHeight: { configurable: true, value: 600 },
    scrollTop: { configurable: true, value: 0, writable: true },
  })
  container.getBoundingClientRect = () => ({ top: 0 } as DOMRect)

  const app = createApp(ChatMessageList, {
    messages: options.messages ?? Array.from({ length: 200 }, (_, index) => message(index)),
    scrollContainer: container,
    shareMode: options.shareMode ?? false,
    forceMountMessageKeys: options.forceMountMessageKeys,
    followLiveEdge: options.followLiveEdge,
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
  app.use(i18n)
  const api = app.mount(host) as unknown as ChatMessageListVirtualizer
  apps.push(app)
  await nextTick()
  await new Promise(resolve => window.requestAnimationFrame(() => resolve(undefined)))
  await nextTick()
  return { api, container, host }
}

beforeEach(() => {
  resizeObserverCallbacks = []
  stubResizeObserver()
  window.localStorage.clear()
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  window.localStorage.clear()
  vi.unstubAllGlobals()
})

describe('ChatMessageList long-history virtualization', () => {
  it('mounts a bounded two-viewport window for a 200-message transcript', async () => {
    const { api, host } = await mountList()

    expect(api.isVirtualized()).toBe(true)
    expect(host.querySelector('.chat-message-list')?.getAttribute('data-virtualized')).toBe('true')
    expect(host.querySelectorAll('[data-testid="chat-message-row"]').length).toBeLessThanOrEqual(30)
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

    const entry = { target: row } as unknown as ResizeObserverEntry
    for (const callback of resizeObserverCallbacks) callback([entry], {} as ResizeObserver)
    await nextTick()
    await nextTick()

    expect(container.scrollTop).toBe(12_000)
  })

  it('keeps the live edge pinned when the chat viewport changes size', async () => {
    const { container } = await mountList({ followLiveEdge: true })
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 12_000 })
    container.scrollTop = 8_400

    const entry = { target: container } as unknown as ResizeObserverEntry
    for (const callback of resizeObserverCallbacks) callback([entry], {} as ResizeObserver)
    await nextTick()
    await nextTick()

    expect(container.scrollTop).toBe(12_000)
  })

  it('leaves a historical reader in place when the chat viewport changes size', async () => {
    const { container } = await mountList({ followLiveEdge: false })
    Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 12_000 })
    container.scrollTop = 8_400

    const entry = { target: container } as unknown as ResizeObserverEntry
    for (const callback of resizeObserverCallbacks) callback([entry], {} as ResizeObserver)
    await nextTick()
    await nextTick()

    expect(container.scrollTop).toBe(8_400)
  })

  it('compensates an above-viewport resize from physical geometry once', async () => {
    const { container, host } = await mountList()
    const row = host.querySelector<HTMLElement>('[data-testid="chat-message-row"]')
    expect(row).toBeTruthy()
    // The user-row estimate is 94px. It grew by 120px; its old bottom was at
    // the viewport edge even though the post-resize box now overlaps it.
    row!.getBoundingClientRect = () => ({ height: 214, bottom: 120 } as DOMRect)

    const entry = { target: row } as unknown as ResizeObserverEntry
    for (const callback of resizeObserverCallbacks) callback([entry], {} as ResizeObserver)
    await nextTick()
    await nextTick()

    expect(container.scrollTop).toBe(120)
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
})
