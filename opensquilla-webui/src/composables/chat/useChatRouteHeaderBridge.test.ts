// @vitest-environment happy-dom
import { createApp, h, ref, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  provideChatRouteHeaderBridge,
  type ChatRouteHeaderBridge,
  type ChatRouteHeaderCommands,
  type ChatRouteHeaderModel,
} from './useChatRouteHeaderBridge'

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

function owner(title: string): {
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
      hasNewDeliverable: ref(false),
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
