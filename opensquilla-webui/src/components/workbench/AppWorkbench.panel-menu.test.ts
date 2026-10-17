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
import { createAssistantChatOwner } from '@/modules/assistantChat'

const pushToast = vi.hoisted(() => vi.fn())
const platform = vi.hoisted(() => ({ current: {} as Platform }))
vi.mock('@/platform', () => ({ usePlatform: () => platform.current }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm: vi.fn(async () => true) }) }))
vi.mock('@/components/ResourceActionsMenu.vue', () => ({ default: { render: () => null } }))
// Keep the actual actions slot, event listeners, store and permission decisions;
// preview engines are outside this menu interaction test.
vi.mock('./WorkbenchHost.vue', async () => {
  const { defineComponent, h } = await import('vue')
  const { useWorkbenchStore } = await import('@/workbench/store')
  return { default: defineComponent({ inheritAttrs: false, props: { beforeCloseItem: Function }, setup(props, { slots, expose }) {
    const store = useWorkbenchStore()
    expose({ hideHover() {} })
    return () => h('section', [slots['tab-actions']?.({ item: store.activeItem }), slots.actions?.({ item: store.activeItem }), !store.activeItem ? slots.empty?.() : null,
      h('button', { 'data-testid': 'close-active-panel', onClick: async () => {
        const item = store.activeItem
        if (item && await props.beforeCloseItem?.(item)) store.closeItem(item.id)
      } }, 'Close'),
    ])
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

beforeEach(() => { localStorage.clear(); workbenchPanelRegistry.clear(); pushToast.mockClear() })
afterEach(async () => {
  apps.splice(0).forEach(app => app.unmount())
  await settle()
  document.body.innerHTML = ''
  workbenchPanelRegistry.clear()
  vi.restoreAllMocks()
})

async function mount(options: { native?: boolean; workspace?: boolean; assistantPorts?: boolean; assistantSupported?: boolean } = {}) {
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
  const props = reactive({ sessionId: 'session-a', routeActive: true, conversationStarted: true, sessionDurable: true, sessionPreparable: true,
    sessionLoading: false,
    enabled: true, modalBlocked: false,
    prepareSession: undefined as ((key: string) => Promise<string | null>) | undefined,
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
  const lifecycle = { supportsSidechat: () => options.assistantSupported !== false, createSidechat: vi.fn(), closeSidechat: vi.fn(async () => ({ key: 'child', closed: true as const })) } as unknown as SessionLifecycle
  if (options.assistantPorts !== false) {
    app.provide(SESSION_LIFECYCLE_KEY, lifecycle)
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
  return { gateway, host, props, store, trigger, open, lifecycle }
}

describe('AppWorkbench new panel menu interaction', () => {
  it.each(['workspace-files'] as const)(
    'materializes a project draft on %s selection before opening its panel', async kind => {
      const harness = await mount({ native: false })
      harness.props.sessionDurable = false
      let finish!: (key: string) => void
      const prepare = vi.fn(() => new Promise<string>(resolve => { finish = resolve }))
      harness.props.prepareSession = prepare
      await harness.open()
      expect(option(kind)?.disabled).toBe(false)
      option(kind)!.click()
      await settle()
      expect(prepare).toHaveBeenCalledExactlyOnceWith('session-a')
      expect(harness.store.items).toHaveLength(1)
      await harness.open()
      expect(option(kind)?.disabled).toBe(true)
      option(kind)!.click()
      expect(prepare).toHaveBeenCalledOnce()
      harness.props.sessionDurable = true
      finish('session-a')
      await settle()
      expect(harness.store.activeItem?.kind).toBe(kind)
      expect(harness.store.activeItem?.scope).toEqual({ type: 'session', id: 'session-a' })
      expect(menu()).toBeNull()
    },
  )

  it.each(['session', 'project', 'connection', 'collapse', 'permission', 'modal', 'feature'] as const)(
    'does not open a stale file panel when %s changes during preparation', async change => {
      const harness = await mount({ native: false })
      harness.props.sessionDurable = false
      let finish!: (key: string) => void
      harness.props.prepareSession = () => new Promise(resolve => { finish = resolve })
      await harness.open()
      option('workspace-files')!.click()
      await settle()
      if (change === 'session') { harness.props.sessionId = 'session-b'; harness.store.setSessionScope('session-b') }
      if (change === 'project') harness.props.workspaceId = 'workspace-b'
      if (change === 'connection') harness.gateway.subscriptionEpoch += 1
      if (change === 'collapse') harness.store.setExpanded(false)
      if (change === 'permission') harness.gateway.isLocalOwner = false
      if (change === 'modal') harness.props.modalBlocked = true
      if (change === 'feature') harness.props.enabled = false
      harness.props.sessionDurable = true
      finish('session-a')
      await settle()
      expect(harness.store.items.some(item => item.kind === 'workspace-files')).toBe(false)
    },
  )

  it.each([false, true])('keeps standalone terminal on drafts but omits side chat (project=%s)', async workspace => {
    const harness = await mount({ native: false, workspace })
    harness.props.sessionDurable = false
    harness.props.conversationStarted = false
    harness.props.sessionPreparable = false
    const prepare = vi.fn(async () => 'session-a')
    harness.props.prepareSession = prepare
    await harness.open()
    expect(option('assistant-chat')).toBeNull()
    expect(option('terminal')?.disabled).toBe(false)
    if (!workspace) expect(option('workspace-files')).toBeNull()
    option('terminal')!.click()
    await settle()
    expect(harness.store.activeItem?.payload.target).toEqual(workspace
      ? { kind: 'project', workspaceId: 'workspace-a' } : { kind: 'default-agent', agentId: 'main' })
    expect(prepare).not.toHaveBeenCalled()
    expect(harness.props.sessionDurable).toBe(false)
  })

  it('requires main-conversation content without retiring an owner while route metadata changes', async () => {
    const harness = await mount()
    harness.props.conversationStarted = false
    await harness.open()
    expect(option('assistant-chat')).toBeNull()
    harness.props.conversationStarted = true
    await settle()
    option('assistant-chat')!.click()
    await settle()
    expect(harness.store.activeItem?.kind).toBe('assistant-chat')
    harness.props.sessionDurable = false
    await settle()
    expect(harness.store.items.some(item => item.kind === 'assistant-chat')).toBe(true)
    harness.props.sessionId = 'new-draft'
    harness.props.conversationStarted = false
    await settle()
    expect(harness.store.visibleItems.some(item => item.kind === 'assistant-chat')).toBe(false)
    harness.store.closeAllItems()
    harness.store.openEmpty()
    await settle()
    expect(harness.host.querySelector('[data-panel-kind="assistant-chat"]')).toBeNull()
  })

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

  it.each(['history-first', 'workspace-first'] as const)(
    'keeps explicitly reopened launcher entries stable while the next session resolves (%s)', async order => {
      const harness = await mount({ native: false })
      harness.store.closeAllItems()
      harness.store.openEmpty()
      await settle()
      const start = harness.host.querySelector('[data-testid="workbench-start-panel"]')!
      const cards = () => [...start.querySelectorAll<HTMLButtonElement>('[data-panel-kind]')]
      const initial = cards()
      harness.props.sessionId = 'session-b'
      harness.props.sessionLoading = true
      harness.props.workspaceResolved = false
      harness.props.workspaceReady = false
      harness.props.workspaceId = null
      harness.props.conversationStarted = false
      await settle()
      expect(harness.store.expanded).toBe(false)
      harness.store.openEmpty()
      await settle()
      expect(harness.store.expanded).toBe(true)
      expect(cards()).toEqual(initial)
      expect(start.getAttribute('aria-busy')).toBe('true')
      for (const card of cards()) { expect(card.disabled).toBe(true); card.click() }
      expect(harness.store.items).toHaveLength(0)
      let historyResolved = false
      const resolveHistory = () => {
        historyResolved = true
        harness.props.conversationStarted = true
        harness.props.sessionLoading = !harness.props.workspaceResolved
      }
      const resolveWorkspace = () => {
        harness.props.workspaceResolved = true
        harness.props.workspaceReady = true
        harness.props.workspaceId = 'workspace-b'
        harness.props.sessionLoading = !historyResolved
      }
      ;(order === 'history-first' ? resolveHistory : resolveWorkspace)()
      await settle()
      expect(cards()).toEqual(initial)
      expect(cards().every(card => card.disabled)).toBe(true)
      ;(order === 'history-first' ? resolveWorkspace : resolveHistory)()
      await settle()
      expect(cards()).toEqual(initial)
      expect(start.getAttribute('aria-busy')).toBe('false')
      const files = cards().find(card => card.dataset.panelKind === 'workspace-files')!
      expect(files.disabled).toBe(false)
      files.click()
      await settle()
      expect(harness.store.activeItem?.scope).toEqual({ type: 'session', id: 'session-b' })
    },
  )

  it('drops inapplicable entries after resolving an empty non-project session', async () => {
    const harness = await mount({ native: false })
    harness.store.closeAllItems()
    harness.store.openEmpty()
    harness.props.sessionId = 'empty-session'
    harness.props.sessionLoading = true
    harness.props.workspaceResolved = false
    harness.props.workspaceId = null
    harness.props.conversationStarted = false
    await settle()
    expect(harness.store.expanded).toBe(false)
    harness.store.openEmpty()
    await settle()
    const start = harness.host.querySelector('[data-testid="workbench-start-panel"]')!
    expect(start.querySelectorAll('[data-panel-kind]')).toHaveLength(4)
    harness.props.sessionLoading = false
    harness.props.workspaceResolved = true
    await settle()
    expect([...start.querySelectorAll<HTMLElement>('[data-panel-kind]')].map(card => card.dataset.panelKind))
      .toEqual(['browser', 'terminal'])
    expect(harness.lifecycle.createSidechat).not.toHaveBeenCalled()
  })

  it('restores independent browser access when workspace resolution fails', async () => {
    const harness = await mount()
    harness.props.sessionLoading = true
    harness.props.workspaceResolved = false
    harness.props.workspaceReady = false
    await harness.open()
    expect(option('browser')?.disabled).toBe(true)
    // Failure ends loading without granting workspace or host access.
    harness.props.sessionLoading = false
    await settle()
    expect(option('browser')?.disabled).toBe(false)
    for (const kind of ['workspace-files', 'terminal', 'assistant-chat'] as const) {
      expect(option(kind)?.disabled).toBe(true)
    }
    option('browser')!.click()
    await settle()
    expect(harness.host.querySelector('[data-testid="browser-start"]')).not.toBeNull()
    expect(harness.lifecycle.createSidechat).not.toHaveBeenCalled()
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

  it.each([true, false])('reports native artifact admission from any producer without stealing focus (activate=%s)', async activate => {
    const harness = await mount()
    const native = (index: number) => ({ id: `native-preview-${index}`, kind: 'artifact-preview' as const,
      title: 'Interactive preview', scope: { type: 'session' as const, id: 'session-a' },
      hostKind: 'native-webcontents' as const, retention: 'keep-alive' as const, payload: {} })
    for (let index = 0; index < 4; index += 1) harness.store.openItem(native(index), { activate: false })
    // Simulates both explicit main/side opens and background agent-produced
    // previews: all enter the store, so none can bypass the shared notification.
    expect(harness.store.openItem(native(4), { activate })).toBe(false)
    await settle()
    expect(harness.store.items).toHaveLength(5)
    expect(harness.store.activeItemId).toBe('existing-panel')
    expect(pushToast).toHaveBeenCalledExactlyOnceWith('workbench.nativePreview.capacityReached',
      { tone: 'warn', duration: 9000 })
    expect(harness.store.openItem({ ...native(0), title: 'Updated preview' })).toBe(true)
    expect(pushToast).toHaveBeenCalledTimes(1)
  })

  it('explains terminal capacity without discarding retained sessions or opening a broken tab', async () => {
    const harness = await mount()
    for (let index = 0; index < 4; index += 1) {
      harness.store.openItem({ id: `retained-terminal-${index}`, kind: 'terminal', title: 'Terminal',
        scope: { type: 'session', id: `old-session-${index}` }, hostKind: 'dom',
        retention: 'keep-alive', payload: {} }, { activate: false })
    }
    await harness.open()
    option('terminal')!.click()
    await settle()
    expect(harness.store.items).toHaveLength(5)
    expect(harness.store.activeItemId).toBe('existing-panel')
    expect(pushToast).toHaveBeenCalledWith('workbench.terminal.capacityReached',
      { tone: 'warn', duration: 6000 })
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
    expect(harness.store.activeItemId).not.toBe(panelId)
    expect(harness.store.items.filter(item => item.kind === 'assistant-chat')).toHaveLength(2)
  })

  it('retains workspace tools across unresolved navigation and restores the original owner', async () => {
    const harness = await mount()
    for (const kind of ['workspace-files', 'terminal'] as const) {
      await harness.open()
      option(kind)!.click()
      await settle()
    }
    const terminalId = harness.store.activeItemId
    const retainedIds = harness.store.items.map(item => item.id)
    harness.props.sessionId = 'session-b'
    harness.props.workspaceResolved = false
    harness.props.workspaceId = null
    await settle()
    harness.props.workspaceId = 'workspace-b'
    harness.props.workspaceResolved = true
    await settle()
    expect(harness.store.items.map(item => item.id)).toEqual(retainedIds)
    harness.props.sessionId = 'session-a'
    harness.props.workspaceResolved = false
    harness.props.workspaceId = null
    await settle()
    expect(harness.store.activeItemId).toBe(terminalId)
    harness.props.workspaceId = 'workspace-a'
    harness.props.workspaceResolved = true
    await settle()
    expect(harness.store.items.map(item => item.id)).toEqual(retainedIds)
    expect(harness.store.activeItemId).toBe(terminalId)
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
    expect(harness.store.items.map(item => item.kind)).toEqual(['artifact-preview', 'assistant-chat'])
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

  it('disables contextual side chat on a Gateway without the dedicated create and close capability', async () => {
    const harness = await mount({ assistantSupported: false })
    await harness.open()
    expect(option('assistant-chat')?.disabled).toBe(true)
    expect(option('assistant-chat')?.querySelector('small')?.textContent).toBe(key('assistantUnavailable'))
  })

  it('retains a side tab until its explicit close has been acknowledged by the Gateway', async () => {
    const harness = await mount()
    await harness.open(); option('assistant-chat')!.click(); await settle()
    const item = harness.store.activeItem!
    vi.mocked(harness.lifecycle.createSidechat!).mockResolvedValue({ key: 'child', sessionId: 'id', parentKey: 'session-a',
      inherited: { model: null, provider: null, reasoningLevel: null, modelRoutingMode: null, modelRoutingRevision: 0, runMode: null },
    })
    const owner = createAssistantChatOwner({ identity: harness.gateway.deliveryIdentity!, parentKey: 'session-a',
      ownerId: String(item.payload.ownerId), lifecycle: harness.lifecycle,
      isCurrent: () => true, ownerAvailable: () => true, onChange: vi.fn(),
    })
    // A side panel is lazy: opening/restoring it must not materialize an
    // empty child. Seed the owner with the first explicit turn, then retire
    // only its reader so the close path exercises the existing child.
    expect((await owner.prepare())?.key).toBe('child')
    owner.dispose()
    vi.mocked(harness.lifecycle.closeSidechat!).mockRejectedValueOnce(new Error('Connection unavailable'))
    harness.host.querySelector<HTMLButtonElement>('[data-testid="close-active-panel"]')!.click(); await settle()
    expect(harness.store.items.some(value => value.id === item.id)).toBe(true)
    expect(harness.lifecycle.closeSidechat).toHaveBeenCalledExactlyOnceWith({ key: 'child' })
    harness.host.querySelector<HTMLButtonElement>('[data-testid="close-active-panel"]')!.click(); await settle()
    expect(harness.store.items.some(value => value.id === item.id)).toBe(false)
    expect(harness.lifecycle.closeSidechat).toHaveBeenCalledTimes(2)
    owner.dispose()
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
    expect(option('assistant-chat')?.querySelector('small')?.textContent).toBe(key('workspaceNotReady'))
    option('assistant-chat')?.click()
    expect(harness.store.items).toHaveLength(1)
    harness.props.workspaceResolved = true
    await settle()
    expect(option('assistant-chat')?.disabled).toBe(false)
  })

  it('keeps the menu open while independent transcript, editor, tabs or menu content scroll', async () => {
    const harness = await mount()
    await harness.open()
    const openMenu = menu()!
    for (const tag of ['div', 'textarea', 'nav']) {
      const unrelated = document.createElement(tag)
      harness.host.append(unrelated)
      unrelated.dispatchEvent(new Event('scroll'))
      await settle()
      expect(menu()).toBe(openMenu)
    }
    openMenu.dispatchEvent(new Event('scroll'))
    await settle()
    expect(menu()).toBe(openMenu)
    expect(harness.store.activeSessionId).toBe('session-a')
  })

  it('repositions on anchor-ancestor scroll and viewport resize without dismissing', async () => {
    const harness = await mount()
    let anchorRight = 340
    let anchorBottom = 50
    vi.spyOn(harness.trigger(), 'getBoundingClientRect').mockImplementation(() => ({
      x: anchorRight - 30, y: anchorBottom - 30, left: anchorRight - 30, top: anchorBottom - 30,
      width: 30, height: 30, right: anchorRight, bottom: anchorBottom, toJSON: () => ({}),
    }))
    await harness.open()
    const openMenu = menu()!
    vi.spyOn(openMenu, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, width: 280, height: 160, right: 280, bottom: 160, toJSON: () => ({}),
    })
    harness.host.dispatchEvent(new Event('scroll'))
    await settle()
    expect(menu()).toBe(openMenu)
    expect(openMenu.style.left).toBe('60px')
    expect(openMenu.style.top).toBe('56px')
    anchorRight = window.innerWidth + 100
    anchorBottom = window.innerHeight - 10
    window.dispatchEvent(new Event('resize'))
    await settle()
    expect(menu()).toBe(openMenu)
    expect(openMenu.style.left).toBe(`${window.innerWidth - 280 - 8}px`)
    expect(openMenu.style.top).toBe(`${window.innerHeight - 160 - 8}px`)
    expect(document.activeElement).toBe(option('browser'))
    harness.trigger().remove()
    window.dispatchEvent(new Event('resize'))
    await settle()
    expect(menu()).toBeNull()
  })

  it.each(['session', 'route', 'collapse', 'blocked'] as const)(
    'still dismisses on %s ownership or visibility changes', async change => {
      const harness = await mount()
      await harness.open()
      expect(menu()).not.toBeNull()
      if (change === 'session') harness.props.sessionId = 'session-b'
      if (change === 'route') harness.props.routeActive = false
      if (change === 'collapse') harness.store.setExpanded(false)
      if (change === 'blocked') harness.props.modalBlocked = true
      await settle()
      expect(menu()).toBeNull()
    },
  )

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
