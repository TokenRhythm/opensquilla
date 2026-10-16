// @vitest-environment happy-dom
import { createApp, h, ref, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  provideChatRouteHeaderBridge,
  type ChatRouteHeaderBridge,
  type ChatRouteHeaderCommands,
  type ChatRouteHeaderModel,
  type ChatRouteHeaderFindState,
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
      sessionKey: ref(`session-${title}`),
      visible: ref(true),
      title: ref(title),
      optimisticTitle: ref(''),
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
      copySessionLink: vi.fn(),
      restoreComposerFocus: vi.fn(),
    },
  }
}

describe('chat route header bridge', () => {
  it('requires a durable session or an explicit preparation command before opening a panel', async () => {
    const bridge = createBridge()
    const draft = owner('draft')
    const durable = ref(false)
    draft.model.sessionDurable = durable
    bridge.register(draft.model, draft.commands)
    expect(bridge.model.sessionDurable.value).toBe(false)
    expect(await bridge.prepareWorkbenchSession('session-draft')).toBeNull()
    durable.value = true
    expect(await bridge.prepareWorkbenchSession('session-draft')).toBe('session-draft')
    expect(await bridge.prepareWorkbenchSession('stale-session')).toBeNull()
  })

  it('prepares a hidden draft without treating header visibility as session availability', async () => {
    const bridge = createBridge()
    const draft = owner('draft')
    const durable = ref(false)
    draft.model.visible = ref(false)
    draft.model.sessionDurable = durable
    draft.commands.prepareWorkbenchSession = vi.fn(async key => {
      durable.value = true
      return key
    })
    bridge.register(draft.model, draft.commands)
    expect(await bridge.prepareWorkbenchSession('stale-session')).toBeNull()
    expect(draft.commands.prepareWorkbenchSession).not.toHaveBeenCalled()
    expect(await bridge.prepareWorkbenchSession('session-draft')).toBe('session-draft')
    expect(draft.commands.prepareWorkbenchSession).toHaveBeenCalledExactlyOnceWith('session-draft')
  })

  it('discards a completed preparation after the route owner is replaced', async () => {
    const bridge = createBridge()
    const first = owner('first')
    first.model.sessionDurable = ref(true)
    let finish!: (key: string) => void
    first.commands.prepareWorkbenchSession = () => new Promise(resolve => { finish = resolve })
    bridge.register(first.model, first.commands)
    const pending = bridge.prepareWorkbenchSession('session-first')
    const replacement = owner('first')
    replacement.model.sessionDurable = ref(true)
    bridge.register(replacement.model, replacement.commands)
    finish('session-first')
    expect(await pending).toBeNull()
  })

  it('fences workspace panel availability to the current registered session', () => {
    const bridge = createBridge()
    const project = owner('project')
    project.model.workspaceId = ref('workspace-a')
    project.model.workspaceReady = ref(true)
    project.model.workspaceResolved = ref(true)
    const previous = bridge.register(project.model, project.commands)
    expect(bridge.model.workspaceId.value).toBe('workspace-a')
    expect(bridge.model.workspaceReady.value).toBe(true)
    expect(bridge.model.workspaceResolved.value).toBe(true)
    const plain = owner('plain')
    bridge.register(plain.model, plain.commands)
    expect(previous.release()).toBe(false)
    expect(bridge.model.workspaceId.value).toBeNull()
    expect(bridge.model.workspaceReady.value).toBe(false)
    expect(bridge.model.workspaceResolved.value).toBe(false)
  })

  it('keeps optional find capability absent for existing header owners', () => {
    const bridge = createBridge()
    const current = owner('old')
    bridge.register(current.model, current.commands)
    expect(bridge.model.findState.value).toBeNull()
    expect(() => bridge.invoke('openFind')).not.toThrow()
    expect(() => bridge.invoke('searchFind', 'needle')).not.toThrow()
  })

  it('routes find state and typed commands only to the currently registered session', () => {
    const bridge = createBridge()
    const first = owner('first')
    first.commands.searchFind = vi.fn()
    const stale = bridge.register(first.model, first.commands)
    const current = owner('current')
    const findState = ref<ChatRouteHeaderFindState>({
      open: true, query: 'needle', resultQuery: 'needle', matchCount: 12, activeMatch: 3,
      status: 'ready', focusSequence: 2, complete: false, coverageComplete: true,
    })
    current.model.findState = findState
    current.commands.updateFindQuery = vi.fn()
    current.commands.searchFind = vi.fn()
    current.commands.navigateFind = vi.fn()
    current.commands.closeFind = vi.fn()
    const registration = bridge.register(current.model, current.commands)
    expect(stale.release()).toBe(false)
    expect(bridge.model.findState.value).toBe(findState.value)
    bridge.invoke('updateFindQuery', 'next')
    bridge.invoke('searchFind', 'next')
    bridge.invoke('navigateFind', 'previous')
    bridge.invoke('closeFind')
    expect(current.commands.updateFindQuery).toHaveBeenCalledWith('next')
    expect(current.commands.searchFind).toHaveBeenCalledWith('next')
    expect(current.commands.navigateFind).toHaveBeenCalledWith('previous')
    expect(current.commands.closeFind).toHaveBeenCalledOnce()
    expect(first.commands.searchFind).not.toHaveBeenCalled()
    registration.release()
    expect(bridge.model.findState.value).toBeNull()
  })

  it('exposes the same draft session while the landing header is hidden and after materialization', () => {
    const bridge = createBridge()
    const draft = owner('draft')
    draft.model.visible = ref(false)
    const registration = bridge.register(draft.model, draft.commands)
    expect(bridge.model.sessionKey.value).toBe('session-draft')
    draft.model.visible = ref(true)
    expect(bridge.model.sessionKey.value).toBe('session-draft')
    registration.release()
    expect(bridge.model.sessionKey.value).toBe('')
  })

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

  it('routes the single copy link action to the active session owner', () => {
    const bridge = createBridge()
    const previous = owner('previous')
    const current = owner('current')
    bridge.register(previous.model, previous.commands)
    bridge.register(current.model, current.commands)

    bridge.invoke('copySessionLink')
    expect(current.commands.copySessionLink).toHaveBeenCalledOnce()
    expect(previous.commands.copySessionLink).not.toHaveBeenCalled()
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
