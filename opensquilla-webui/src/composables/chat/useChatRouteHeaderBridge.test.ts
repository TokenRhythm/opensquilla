// @vitest-environment happy-dom
import { createApp, h, ref, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  provideChatRouteHeaderBridge,
  type ChatRouteHeaderBridge,
  type ChatRouteHeaderCommands,
  type ChatRouteHeaderModel,
} from './useChatRouteHeaderBridge'
import type { ContextUsage } from './useChatUsageWidget'

let mountedRoot: HTMLElement | null = null
let mountedApp: App<Element> | null = null

afterEach(() => {
  mountedApp?.unmount()
  mountedApp = null
  mountedRoot?.remove()
  mountedRoot = null
})

function createBridge(): ChatRouteHeaderBridge {
  let bridge: ChatRouteHeaderBridge | null = null
  mountedRoot = document.createElement('div')
  document.body.appendChild(mountedRoot)
  mountedApp = createApp({
    setup() {
      bridge = provideChatRouteHeaderBridge()
      return () => h('div')
    },
  })
  mountedApp.mount(mountedRoot)
  if (!bridge) throw new Error('bridge setup failed')
  return bridge
}

function owner(
  title: string,
  contextUsage: ContextUsage | null = null,
): {
  model: ChatRouteHeaderModel
  commands: ChatRouteHeaderCommands
} {
  return {
    model: {
      visible: ref(true),
      title: ref(title),
      copyState: ref(null),
      copyIcon: ref('copy'),
      copyLiveText: ref(''),
      deliverableCount: ref(0),
      contextUsage: ref(contextUsage),
      shareMode: ref(false),
      shareableMessageCount: ref(1),
    },
    commands: {
      openDeliverables: vi.fn(),
      startShare: vi.fn(),
      copySessionKey: vi.fn(),
      restoreComposerFocus: vi.fn(),
    },
  }
}

describe('chat route header bridge', () => {
  it('keeps a newer owner when stale teardown arrives', () => {
    const bridge = createBridge()
    const first = owner('first')
    const second = owner('second')
    const firstRegistration = bridge.register(first.model, first.commands)
    const secondRegistration = bridge.register(second.model, second.commands)

    expect(secondRegistration.ownerToken).toBeGreaterThan(firstRegistration.ownerToken)
    expect(firstRegistration.release()).toBe(false)
    expect(bridge.model.title.value).toBe('second')

    bridge.invoke('startShare')
    expect(second.commands.startShare).toHaveBeenCalledOnce()
    expect(first.commands.startShare).not.toHaveBeenCalled()
  })

  it('closes host state and hides the model when the active owner clears', () => {
    const bridge = createBridge()
    const current = owner('current')
    const closeMenu = vi.fn()
    const focusAction = vi.fn(() => true)
    bridge.setHost({ closeMenu, focusAction })
    const registration = bridge.register(current.model, current.commands)

    expect(registration.focusAction('share')).toBe(true)
    expect(focusAction).toHaveBeenCalledWith('share')
    expect(registration.release()).toBe(true)
    expect(closeMenu).toHaveBeenCalled()
    expect(bridge.model.visible.value).toBe(false)
    expect(bridge.model.title.value).toBe('')
  })

  it('publishes the context reading and drops it with its owner', () => {
    // The reading is the one header field App cannot recompute on its own: it
    // lives in the chat session, so it has to survive the bridge hop, and it
    // has to disappear the moment no session owns the header — a stale
    // percentage beside a new title reads as that session's usage.
    const bridge = createBridge()
    const usage: ContextUsage = { pct: 87, usedK: 87, windowK: 100, warning: true }
    const current = owner('current', usage)
    const registration = bridge.register(current.model, current.commands)

    expect(bridge.model.contextUsage.value).toEqual(usage)

    registration.release()

    expect(bridge.model.contextUsage.value).toBeNull()
  })

  it('closes the mounted menu when the registered view returns to landing', () => {
    const bridge = createBridge()
    const current = owner('current')
    const closeMenu = vi.fn()
    bridge.setHost({ closeMenu, focusAction: () => false })
    bridge.register(current.model, current.commands)

    ;(current.model.visible as { value: boolean }).value = false

    expect(closeMenu).toHaveBeenCalled()
    expect(bridge.model.visible.value).toBe(false)
  })
})
