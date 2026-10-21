// @vitest-environment happy-dom
import { createApp, computed, defineComponent, h, nextTick, reactive, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { provideChatRouteHeaderBridge, type ChatRouteHeaderBridge } from '@/composables/chat/useChatRouteHeaderBridge'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { SESSION_LIFECYCLE_KEY, type SessionLifecycle } from '@/modules/sessionLifecycle'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY } from '@/modules/sessionReadLifecycle'
import { TURN_COMMANDS_KEY } from '@/modules/turnCommands'
import { CONVERSATION_EVENTS_KEY } from '@/modules/conversationEvents'
import { closeAssistantChatOwner } from '@/modules/assistantChat'
import { useWorkbenchStore } from '@/workbench/store'
import { workbenchPanelRegistry } from '@/workbench/registry'
import AppWorkbench from './AppWorkbench.vue'

// This suite verifies AppWorkbench route ownership with the real side owner.
// Transcript/draft/file behavior is exercised by the real routed-host E2E.
const { views } = vi.hoisted(() => ({ views: [] as {
  props: Record<string, unknown> & { prepareSession: () => Promise<string> }
  unmounted: boolean
}[] }))
vi.mock('@/views/ChatView.vue', async () => {
  const { defineComponent, h, onUnmounted } = await import('vue')
  return { default: defineComponent({
    props: ['sessionKeyOverride', 'workbenchOwnerSessionKey', 'prepareSession', 'draftIdentity', 'draftStorage'],
    setup(props) {
      const view = { props: props as unknown as typeof views[number]['props'], unmounted: false }
      views.push(view)
      onUnmounted(() => { view.unmounted = true })
      return () => h('div', { 'data-testid': 'shared-chat-view' })
    },
  }) }
})
vi.mock('@/platform', () => ({ usePlatform: () => ({ capabilities: { hasNativeWorkbenchSurfaces: false }, workbench: {} }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm: vi.fn(async () => true) }) }))
vi.mock('@/components/ResourceActionsMenu.vue', () => ({ default: { render: () => null } }))
vi.mock('./artifactWorkbenchProvider', () => ({ createArtifactWorkbenchDefinitions: () => [] }))
vi.mock('./browserWorkbenchProvider', () => ({ createBrowserWorkbenchDefinition: () => ({ kind: 'browser' }) }))
vi.mock('./workbenchResourceCollectionProvider', () => ({ createWorkbenchResourceCollectionDefinition: () => ({ kind: 'resource-collection' }) }))
vi.mock('./workspaceFilesWorkbenchProvider', () => ({ createWorkspaceFilesWorkbenchDefinition: () => ({ kind: 'workspace-files' }) }))
vi.mock('./terminalWorkbenchProvider', () => ({ createTerminalWorkbenchDefinition: () => ({ kind: 'terminal' }) }))
vi.mock('./toolHistoryFileProvider', () => ({ createToolHistoryFileDefinition: () => ({ kind: 'tool-file-history' }) }))

const apps: App[] = []
const cleanups: (() => Promise<void>)[] = []
async function settle() { for (let index = 0; index < 12; index++) await nextTick() }
afterEach(async () => {
  apps.splice(0).forEach(app => app.unmount())
  for (const cleanup of cleanups.splice(0)) await cleanup()
  await settle()
  views.splice(0)
  document.body.innerHTML = ''
  workbenchPanelRegistry.clear()
  localStorage.clear()
})

async function mountRouteWorkbench() {
  const parentKey = `parent-${crypto.randomUUID()}`
  const identity = crypto.randomUUID()
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat', component: { render: () => null } },
    { path: '/chat/new', component: { render: () => null } },
  ] })
  await router.push({ path: '/chat', query: { session: parentKey } })
  const pinia = createPinia()
  const store = useWorkbenchStore(pinia)
  let bridge!: ChatRouteHeaderBridge
  const Root = defineComponent(() => {
    bridge = provideChatRouteHeaderBridge()
    const sessionKey = computed(() => String(router.currentRoute.value.query.session || bridge.model.sessionKey.value))
    return () => h(AppWorkbench, {
      routeActive: true, sessionId: sessionKey.value,
      sessionDurable: bridge.model.sessionKey.value === sessionKey.value && bridge.model.sessionDurable.value,
      conversationStarted: bridge.model.sessionKey.value === sessionKey.value && bridge.model.sessionDurable.value
        && bridge.model.visible.value && bridge.model.shareableMessageCount.value > 0,
      workspaceResolved: bridge.model.sessionKey.value === sessionKey.value && bridge.model.workspaceResolved.value,
    })
  })
  const create = vi.fn(async () => ({ key: 'child', parentKey, sessionId: 'child-id', inherited: {
    model: null, provider: null, reasoningLevel: null, modelRoutingMode: null, modelRoutingRevision: 0, runMode: 'safe',
  } }))
  const close = vi.fn(async () => ({ key: 'child', closed: true as const }))
  const lifecycle = { supportsSidechat: () => true, createSidechat: create, closeSidechat: close } as unknown as SessionLifecycle
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp(Root)
  app.use(pinia); app.use(router); app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, reactive({ availability: 'available', isAvailable: true, connectionHealth: 'healthy', connectionPhase: 'healthy',
    deliveryIdentity: identity, subscriptionEpoch: 1, isLocalOwner: true }) as unknown as GatewayAccess)
  app.provide(SESSION_LIFECYCLE_KEY, lifecycle)
  // AppWorkbench checks that these capabilities exist before offering a side
  // tab. The probe never calls them: no mock execution/runtime is installed.
  app.provide(SESSION_READ_LIFECYCLE_FACTORY_KEY, {} as never)
  app.provide(TURN_COMMANDS_KEY, {} as never)
  app.provide(CONVERSATION_EVENTS_KEY, {} as never)
  app.provide(ARTIFACT_WORKBENCH_KEY, { documents: null, resources: null, content: {}, previews: {},
    subscribeDocumentChanges: () => ({ close() {} }) } as unknown as ArtifactWorkbench)
  app.mount(host); apps.push(app)
  function publish(key: string, durable: boolean) {
    return bridge.register({
      sessionKey: ref(key), sessionDurable: computed(() => durable && router.currentRoute.value.path !== '/chat/new'),
      visible: ref(durable), workspaceResolved: ref(true),
      title: ref('Chat'), optimisticTitle: ref(''), copyState: ref(null), copyIcon: ref('copy'), copyLiveText: ref(''),
      deliverableCount: ref(0), hasNewDeliverable: ref(false), shareMode: ref(false), shareableMessageCount: ref(durable ? 2 : 0),
    }, { openDeliverables: vi.fn(), startShare: vi.fn(), copySessionLink: vi.fn(), restoreComposerFocus: vi.fn() })
  }
  publish(parentKey, true)
  await settle()
  store.openEmpty(); await settle()
  host.querySelector<HTMLButtonElement>('[data-testid="workbench-start-panel"] [data-panel-kind="assistant-chat"]')!.click()
  await settle()
  const item = store.activeItem!
  cleanups.push(() => closeAssistantChatOwner({ identity, parentKey, ownerId: String(item.payload.ownerId || item.id), lifecycle }))
  return { router, bridge, store, host, parentKey, publish, create, close, item, view: views[views.length - 1]! }
}

describe('AppWorkbench side owner route visibility', () => {
  it.each([false, true])('retains the side owner across New task and an unresolved return (materialized: %s)', async materialized => {
    const f = await mountRouteWorkbench()
    if (materialized) expect(await f.view.props.prepareSession()).toBe('child')
    await settle()
    const binding = f.view.props.draftIdentity
    const storage = f.view.props.draftStorage
    await f.router.push('/chat/new')
    f.bridge.clear(); await settle()
    f.publish('new-draft', false); await settle()
    expect(f.store.visibleItems.some(value => value.kind === 'assistant-chat')).toBe(false)
    const sideLayer = f.host.querySelector<HTMLElement>(`[data-workbench-item-id="${f.item.id}"]`)!
    expect(sideLayer.style.display).toBe('none')
    expect(sideLayer.getAttribute('aria-hidden')).toBe('true')
    expect(f.host.querySelector('[data-testid="workbench-start-panel"] [data-panel-kind="assistant-chat"]')).toBeNull()
    expect(f.store.items).toContainEqual(f.item)

    // URL identity returns before the route header can attest durability.
    await f.router.push({ path: '/chat', query: { session: f.parentKey } })
    f.bridge.clear(); await settle()
    expect(f.store.items).toContainEqual(f.item)
    f.publish(f.parentKey, true); await settle()
    expect(f.store.activeItemId).toBe(f.item.id)
    expect(f.store.expanded).toBe(true)
    expect(sideLayer.style.display).not.toBe('none')
    expect(f.view.unmounted).toBe(false)
    expect(views).toHaveLength(1)
    expect(f.view.props.draftIdentity).toBe(binding)
    expect(f.view.props.draftStorage).toBe(storage)
    expect(f.view.props.sessionKeyOverride).toBe(materialized ? 'child' : '')
    expect(f.create).toHaveBeenCalledTimes(materialized ? 1 : 0)
    expect(f.close).not.toHaveBeenCalled()
    expect(f.router.currentRoute.value.query.session).toBe(f.parentKey)
  })
})
