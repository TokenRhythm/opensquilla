// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, reactive, type App } from 'vue'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { WORKSPACE_FILES_KEY, type WorkspaceFiles } from '@/modules/workspaceFiles'
import { TERMINAL_ACCESS_KEY, type TerminalAccess } from '@/modules/terminal'
import { SESSION_LIFECYCLE_KEY, type SessionLifecycle } from '@/modules/sessionLifecycle'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY, type SessionReadLifecycleFactory } from '@/modules/sessionReadLifecycle'
import { TURN_COMMANDS_KEY, type TurnCommands } from '@/modules/turnCommands'
import { CONVERSATION_EVENTS_KEY, type ConversationEvents } from '@/modules/conversationEvents'
import type { Platform } from '@/platform/types'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import { useWorkbenchStore } from '@/workbench/store'
import { workbenchPanelRegistry } from '@/workbench/registry'
import AppWorkbench from './AppWorkbench.vue'

const platform = vi.hoisted(() => ({ current: {} as Platform }))
vi.mock('@/platform', () => ({ usePlatform: () => platform.current }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm: vi.fn(async () => true) }) }))
vi.mock('@/components/ResourceActionsMenu.vue', () => ({ default: { render: () => null } }))
// Keep the actual actions slot, event listeners, store and permission decisions;
// preview engines are outside this menu interaction test.
vi.mock('./WorkbenchHost.vue', async () => {
  const { defineComponent, h } = await import('vue')
  const { useWorkbenchStore } = await import('@/workbench/store')
  return { default: defineComponent({ inheritAttrs: false, setup(_, { slots, expose }) {
    const store = useWorkbenchStore()
    expose({ hideHover() {} })
    return () => h('section', [slots.actions?.({ item: store.activeItem }), !store.activeItem ? slots.empty?.() : null])
  } }) }
})
vi.mock('./artifactWorkbenchProvider', () => ({ createArtifactWorkbenchDefinitions: () => [] }))
vi.mock('./browserWorkbenchProvider', () => ({ createBrowserWorkbenchDefinition: () => ({ kind: 'browser' }) }))
vi.mock('./workbenchResourceCollectionProvider', () => ({ createWorkbenchResourceCollectionDefinition: () => ({ kind: 'resource-collection' }) }))
vi.mock('./workspaceFileProvider', () => ({ createWorkspaceFileDefinition: () => ({ kind: 'file' }) }))
vi.mock('./workspaceFilesWorkbenchProvider', () => ({ createWorkspaceFilesWorkbenchDefinition: () => ({ kind: 'workspace-files' }) }))
vi.mock('./terminalWorkbenchProvider', () => ({ createTerminalWorkbenchDefinition: () => ({ kind: 'terminal' }) }))
vi.mock('./assistantChatWorkbenchProvider', () => ({ createAssistantChatWorkbenchDefinition: () => ({ kind: 'assistant-chat' }) }))
vi.mock('./toolHistoryFileProvider', () => ({ createToolHistoryFileDefinition: () => ({ kind: 'tool-file-history' }) }))

const apps: App[] = []
const key = (suffix: string) => `workbench.newPanel.${suffix}`
type PanelKind = 'browser' | 'workspace-files' | 'terminal' | 'assistant-chat'
type GatewayMenuFixture = { -readonly [K in keyof Pick<GatewayAccess,
  'availability' | 'isAvailable' | 'connectionHealth' | 'connectionPhase'
  | 'isResuming' | 'canExecuteHost' | 'hostExecuteUnavailableReason' | 'isLocalOwner'
  | 'isAuthenticated' | 'subscriptionEpoch' | 'deliveryIdentity'>]: GatewayAccess[K] }
const menu = () => document.querySelector<HTMLElement>('[data-testid="workbench-new-panel-menu-list"]')
const option = (kind: PanelKind) => menu()?.querySelector<HTMLButtonElement>(`[data-panel-kind="${kind}"]`) ?? null
async function settle() { for (let index = 0; index < 5; index++) { await Promise.resolve(); await nextTick() } }

beforeEach(() => { localStorage.clear(); workbenchPanelRegistry.clear() })
afterEach(async () => {
  apps.splice(0).forEach(app => app.unmount())
  await settle()
  document.body.innerHTML = ''
  workbenchPanelRegistry.clear()
  vi.restoreAllMocks()
})

async function mount(options: { native?: boolean; workspace?: boolean; assistantPorts?: boolean } = {}) {
  platform.current = { capabilities: { hasNativeWorkbenchSurfaces: options.native !== false },
    workbench: { native: options.native === false ? undefined : {
      onSurfaceEvent: () => () => {}, setSurfaceRect: vi.fn(async () => {}), destroySurface: vi.fn(async () => {}),
    } },
  } as unknown as Platform
  const gateway = reactive({
    availability: 'available', isAvailable: true, connectionHealth: 'healthy', connectionPhase: 'healthy',
    isResuming: false, canExecuteHost: true, hostExecuteUnavailableReason: null,
    isLocalOwner: true, isAuthenticated: true, subscriptionEpoch: 1, deliveryIdentity: 'verified-connection-a',
  } as GatewayMenuFixture)
  const props = reactive({ sessionId: 'session-a', routeActive: true,
    workspaceId: options.workspace === false ? null : 'workspace-a', workspaceReady: true, workspaceResolved: true })
  const pinia = createPinia()
  const store = useWorkbenchStore(pinia)
  store.setSessionScope(props.sessionId)
  store.openItem({ id: 'existing-panel', kind: 'artifact-preview', title: 'Existing preview',
    scope: { type: 'session', id: props.sessionId }, hostKind: 'dom', retention: 'keep-alive', payload: {} })
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(defineComponent(() => () => h(AppWorkbench, props)))
  app.use(pinia)
  app.provide(GATEWAY_ACCESS_KEY, gateway as GatewayAccess)
  app.provide(WORKSPACE_FILES_KEY, { resolve: vi.fn(), read: vi.fn(), listDir: vi.fn() } as unknown as WorkspaceFiles)
  app.provide(TERMINAL_ACCESS_KEY, {} as TerminalAccess)
  if (options.assistantPorts !== false) {
    app.provide(SESSION_LIFECYCLE_KEY, {} as SessionLifecycle)
    app.provide(SESSION_READ_LIFECYCLE_FACTORY_KEY, {} as SessionReadLifecycleFactory)
    app.provide(TURN_COMMANDS_KEY, {} as TurnCommands)
    app.provide(CONVERSATION_EVENTS_KEY, {} as ConversationEvents)
  }
  app.provide(ARTIFACT_WORKBENCH_KEY, { documents: null, resources: null, content: {}, previews: {},
    subscribeDocumentChanges: () => ({ close() {} }),
  } as unknown as ArtifactWorkbench)
  app.mount(host)
  apps.push(app)
  await settle()
  const trigger = () => host.querySelector<HTMLButtonElement>('[data-testid="workbench-new-panel-trigger"]')!
  const open = async () => { trigger().click(); await settle() }
  return { gateway, host, props, store, trigger, open }
}

describe('AppWorkbench new panel menu interaction', () => {
  it('starts an empty remote Workbench with panel cards and their permission states', async () => {
    const harness = await mount({ native: false })
    harness.store.closeAllItems()
    harness.store.openEmpty()
    await settle()
    const start = harness.host.querySelector('[data-testid="workbench-start-panel"]')!
    expect(start).not.toBeNull()
    expect(harness.host.querySelector('[data-testid="browser-start"]')).toBeNull()
    expect(harness.host.querySelector('input[inputmode="url"]')).toBeNull()
    expect(start.querySelectorAll('[data-panel-kind]')).toHaveLength(4)
    expect(start.querySelector<HTMLButtonElement>('[data-panel-kind="browser"]')?.disabled).toBe(true)
    expect(start.querySelector<HTMLButtonElement>('[data-panel-kind="terminal"]')?.disabled).toBe(false)
    harness.gateway.canExecuteHost = false
    harness.gateway.hostExecuteUnavailableReason = 'permission'
    await settle()
    const terminal = start.querySelector<HTMLButtonElement>('[data-panel-kind="terminal"]')!
    expect(terminal.disabled).toBe(true)
    expect(terminal.querySelector('small')?.textContent).toBe(key('requiresHostExecute'))
  })

  it('shows the URL form only after choosing Browser and restores cards when its last tab closes', async () => {
    const harness = await mount()
    harness.store.closeAllItems()
    harness.store.openEmpty()
    await settle()
    harness.host.querySelector<HTMLButtonElement>('[data-testid="workbench-start-panel"] [data-panel-kind="browser"]')!.click()
    await settle()
    expect(harness.host.querySelector('[data-testid="workbench-start-panel"]')).toBeNull()
    const address = harness.host.querySelector<HTMLInputElement>('[data-testid="browser-start"] input')!
    expect(address).not.toBeNull()
    address.value = 'https://example.com'
    address.dispatchEvent(new Event('input', { bubbles: true }))
    harness.host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await settle()
    expect(harness.store.activeItem?.kind).toBe('browser')
    expect(harness.store.items).toHaveLength(1)
    harness.store.closeItem(harness.store.activeItemId!)
    await settle()
    expect(harness.host.querySelector('[data-testid="workbench-start-panel"]')).not.toBeNull()
    expect(harness.host.querySelector('[data-testid="browser-start"]')).toBeNull()
  })

  it('keeps the plus beside an existing panel and omits denial copy on available entries', async () => {
    const harness = await mount()
    expect(harness.trigger()).not.toBeNull()
    await harness.open()
    expect(menu()?.parentElement).toBe(document.body)
    expect(menu()?.querySelectorAll('[role="menuitem"]')).toHaveLength(4)
    for (const kind of ['browser', 'workspace-files', 'terminal', 'assistant-chat'] as const) {
      expect(option(kind)?.disabled).toBe(false)
      expect(option(kind)?.querySelector('small')).toBeNull()
    }
  })

  it.each(['unavailable', 'preparing', 'checking', 'suspect', 'resuming'] as const)(
    'disables Gateway-backed panels while the connection is %s', async state => {
      const harness = await mount()
      if (state === 'checking') harness.gateway.connectionPhase = 'checking'
      else if (state === 'suspect') harness.gateway.connectionHealth = 'suspect'
      else if (state === 'resuming') harness.gateway.isResuming = true
      else {
        harness.gateway.availability = state
        // A stale connected transport flag must not overrule preparing.
        harness.gateway.isAvailable = state === 'preparing'
      }
      await harness.open()
      for (const kind of ['workspace-files', 'terminal', 'assistant-chat'] as const) {
        expect(option(kind)?.disabled).toBe(true)
        expect(option(kind)?.querySelector('small')?.textContent).toBe(key('requiresConnection'))
        option(kind)?.click()
      }
      expect(harness.store.items).toHaveLength(1)
      expect(option('browser')?.disabled).toBe(false)
    },
  )

  it('requires host.execute even for an owner and updates denial copy without reopening', async () => {
    const harness = await mount()
    harness.gateway.canExecuteHost = false
    harness.gateway.hostExecuteUnavailableReason = 'permission'
    await harness.open()
    expect(option('terminal')?.disabled).toBe(true)
    expect(option('terminal')?.querySelector('small')?.textContent).toBe(key('requiresHostExecute'))
    option('terminal')?.click()
    expect(harness.store.items).toHaveLength(1)
    harness.gateway.hostExecuteUnavailableReason = 'disconnected'
    await settle()
    expect(option('terminal')?.querySelector('small')?.textContent).toBe(key('requiresConnection'))
    harness.gateway.canExecuteHost = true
    harness.gateway.hostExecuteUnavailableReason = null
    await settle()
    expect(option('terminal')?.disabled).toBe(false)
    expect(option('terminal')?.querySelector('small')).toBeNull()
  })

  it('keeps remote terminal available but disables a side browser without native surfaces', async () => {
    const harness = await mount({ native: false })
    harness.gateway.isLocalOwner = false
    await harness.open()
    expect(option('browser')?.disabled).toBe(true)
    expect(option('browser')?.querySelector('small')?.textContent).toBe(key('browserUnavailable'))
    expect(option('terminal')?.disabled).toBe(false)
    expect(option('workspace-files')?.disabled).toBe(true)
  })

  it('hides files for an ordinary chat and disables an unresolved workspace binding', async () => {
    const harness = await mount({ workspace: false })
    await harness.open()
    expect(option('workspace-files')).toBeNull()
    harness.props.workspaceId = 'workspace-a'
    harness.props.workspaceReady = false
    await settle()
    expect(option('workspace-files')?.disabled).toBe(true)
    expect(option('workspace-files')?.querySelector('small')).not.toBeNull()
  })

  it('opens workspace and terminal panels without closing the existing preview', async () => {
    const harness = await mount()
    await harness.open()
    option('workspace-files')!.click()
    await settle()
    expect(menu()).toBeNull()
    expect(harness.store.activeItem?.kind).toBe('workspace-files')
    expect(harness.store.items.some(item => item.id === 'existing-panel')).toBe(true)
    await harness.open()
    option('terminal')!.click()
    await settle()
    expect(harness.store.activeItem?.kind).toBe('terminal')
    expect(harness.store.items).toHaveLength(3)
    expect(harness.trigger()).not.toBeNull()
  })

  it('opens an assistant panel scoped to the parent without changing the main conversation', async () => {
    const harness = await mount()
    const parentKey = harness.store.activeSessionId
    const location = window.location.href
    await harness.open()
    option('assistant-chat')!.click()
    await settle()
    expect(menu()).toBeNull()
    expect(harness.store.activeItem).toMatchObject({ kind: 'assistant-chat',
      scope: { type: 'session', id: parentKey },
      payload: { parentSessionKey: parentKey, workspaceId: 'workspace-a' },
    })
    expect(harness.store.activeSessionId).toBe(parentKey)
    expect(window.location.href).toBe(location)
    expect(harness.store.items.some(item => item.id === 'existing-panel')).toBe(true)
    const panelId = harness.store.activeItemId
    await harness.open()
    option('assistant-chat')!.click()
    await settle()
    expect(harness.store.activeItemId).toBe(panelId)
    expect(harness.store.items.filter(item => item.kind === 'assistant-chat')).toHaveLength(1)
  })

  it('retires workspace-bound panels when the parent changes workspace', async () => {
    const harness = await mount()
    for (const kind of ['workspace-files', 'terminal', 'assistant-chat'] as const) {
      await harness.open()
      option(kind)!.click()
      await settle()
    }
    expect(harness.store.items).toHaveLength(4)
    harness.props.workspaceId = 'workspace-b'
    await settle()
    expect(harness.store.items.map(item => item.id)).toEqual(['existing-panel'])
    await harness.open()
    option('assistant-chat')!.click()
    await settle()
    expect(harness.store.activeItem?.payload).toMatchObject({ workspaceId: 'workspace-b' })
  })

  it('disables assistant chat when its domain ports or verified connection identity are absent', async () => {
    const harness = await mount({ assistantPorts: false })
    await harness.open()
    expect(option('assistant-chat')?.disabled).toBe(true)
    expect(option('assistant-chat')?.querySelector('small')?.textContent).toBe(key('assistantUnavailable'))
    option('assistant-chat')?.click()
    expect(harness.store.items).toHaveLength(1)
  })

  it('does not enable assistant chat until connection identity is proven', async () => {
    const harness = await mount()
    harness.gateway.deliveryIdentity = null
    await harness.open()
    expect(option('assistant-chat')?.disabled).toBe(true)
    harness.gateway.deliveryIdentity = 'verified-connection-a'
    await settle()
    expect(option('assistant-chat')?.disabled).toBe(false)
    expect(option('assistant-chat')?.querySelector('small')).toBeNull()
  })

  it('does not create a default assistant conversation before workspace metadata is resolved', async () => {
    const harness = await mount({ workspace: false })
    harness.props.workspaceResolved = false
    await harness.open()
    expect(option('assistant-chat')?.disabled).toBe(true)
    expect(option('assistant-chat')?.querySelector('small')?.textContent).toBe(key('workspaceUnavailable'))
    option('assistant-chat')?.click()
    expect(harness.store.items).toHaveLength(1)
    harness.props.workspaceResolved = true
    await settle()
    expect(option('assistant-chat')?.disabled).toBe(false)
  })

  it('navigates by keyboard and restores focus after dismissal while occluding native surfaces', async () => {
    const harness = await mount()
    harness.gateway.canExecuteHost = false
    harness.gateway.hostExecuteUnavailableReason = 'permission'
    const occluded = useNativeSurfaceOcclusionState()
    harness.trigger().focus()
    harness.trigger().dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    await settle()
    expect(document.activeElement).toBe(option('browser'))
    expect(occluded.value).toBe(true)
    option('browser')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(option('assistant-chat'))
    option('assistant-chat')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(option('workspace-files'))
    option('workspace-files')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(menu()).toBeNull()
    expect(document.activeElement).toBe(harness.trigger())
    expect(occluded.value).toBe(false)
    await harness.open()
    menu()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    await settle()
    expect(menu()).toBeNull()
    await harness.open()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await settle()
    expect(menu()).toBeNull()
  })
})
